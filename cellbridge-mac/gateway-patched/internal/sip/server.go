package sip

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/cellbridge/cellbridge/gateway/internal/modem"
	"github.com/google/uuid"
)

type Server struct {
	listenAddr string
	conn       *net.UDPConn
	registrar  *Registrar
	auth       *Auth
	modem      *modem.ActiveCallAdapter
	audio      modem.VoiceAudio
	events     <-chan modem.ModemEvent
	sessions   sync.Map
	pushToken  string
	sendSMS    func(ctx context.Context, to, body string) error
	ctx        context.Context
	cancel     context.CancelFunc
}

func NewServer(listenAddr string, registrar *Registrar, auth *Auth, modemCtl *modem.ActiveCallAdapter, audio modem.VoiceAudio) *Server {
	ctx, cancel := context.WithCancel(context.Background())
	return &Server{listenAddr: listenAddr, registrar: registrar, auth: auth, modem: modemCtl, audio: audio, ctx: ctx, cancel: cancel}
}

// AttachEvents wires modem events (RING etc) and the YakPhone push token
// from config so inbound cellular calls can ring the SIP client (§21).
func (s *Server) AttachEvents(events <-chan modem.ModemEvent, pushToken string) {
	s.events = events
	s.pushToken = pushToken
}

// AttachSMS wires the SMS engine so SIP MESSAGE requests from the phone
// are delivered over the cellular modem (final architecture: SMS also
// routes through the NAS Gateway).
func (s *Server) AttachSMS(send func(ctx context.Context, to, body string) error) {
	s.sendSMS = send
}

func (s *Server) Start(ctx context.Context) error {
	addr, err := net.ResolveUDPAddr("udp", s.listenAddr)
	if err != nil {
		return err
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return err
	}
	s.conn = conn
	slog.Info("sip server listening", "addr", s.listenAddr)
	go s.readLoop()
	go s.inboundLoop()
	return nil
}

func (s *Server) Stop(ctx context.Context) error {
	s.cancel()
	if s.conn != nil {
		_ = s.conn.Close()
	}
	s.sessions.Range(func(k, v interface{}) bool { _ = v.(*SIPCallSession).Hangup(); return true })
	return nil
}

func (s *Server) AddUser(username, password string) { s.auth.AddUser(username, password) }

func (s *Server) readLoop() {
	buf := make([]byte, 8192)
	for {
		n, remote, err := s.conn.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-s.ctx.Done():
				return
			default:
			}
			continue
		}
		msg := string(buf[:n])
		go s.handleMessage(msg, remote)
	}
}

// inboundLoop watches modem events. On an incoming cellular call (RING /
// +CLIP), it sends a SIP INVITE to every registered client (§21) and fires
// the YakPhone PushKit notification so the phone wakes even when the app
// is suspended.
func (s *Server) inboundLoop() {
	for {
		select {
		case <-s.ctx.Done():
			return
		case event, ok := <-s.events:
			if !ok {
				return
			}
			switch event.Kind {
			case "incoming":
				go s.ringClients(event)
			case "ended":
				go s.endInboundCall(event)
			}
		}
	}
}

// endInboundCall releases the session for a cellular call the network or
// the far end already tore down. Without it the inbound session (and its
// media port) leaked for the rest of the gateway's lifetime.
func (s *Server) endInboundCall(event modem.ModemEvent) {
	callID := "in-" + string(event.CallID)
	v, ok := s.sessions.Load(callID)
	if !ok {
		return
	}
	sess, ok := v.(*SIPCallSession)
	if !ok {
		return
	}
	slog.Info("sip inbound ended by modem", "call", callID)
	s.sessions.Delete(callID)
	_ = sess.Hangup()
}

func (s *Server) ringClients(event modem.ModemEvent) {
	peer := event.Peer
	if peer == "" {
		peer = "unknown"
	}
	// Key the session on the modem's physical call id. A cellular call
	// rings repeatedly (one RING every few seconds) and the upstream code
	// minted a brand new session per RING, blasting the client with
	// parallel INVITEs for a single incoming call.
	callID := "in-" + string(event.CallID)
	if callID == "in-" {
		callID = "in-" + uuid.NewString()[:12]
	}
	if _, ok := s.sessions.Load(callID); ok {
		return
	}
	media, err := NewMediaSession("0.0.0.0:0")
	if err != nil {
		return
	}
	sess := NewSIPCallSession(callID, peer, "inbound", s.modem, s.audio, media)
	s.sessions.Store(callID, sess)
	sent := 0
	// Address the phone can actually reach back on. nasIP() only knows the
	// tailnet (100.x) address and degrades to 127.0.0.1 without Tailscale, so
	// the VoIP push used to advertise sip:<peer>@127.0.0.1 — YakPhone then woke
	// for a CallKit call whose URI pointed at the phone itself. Prefer the
	// interface used to reach the registered client.
	reachable := ""
	for attempt := 0; attempt < inboundInviteAttempts && sent == 0; attempt++ {
		if attempt > 0 {
			select {
			case <-sess.ctx.Done():
				return
			case <-time.After(inboundInviteRetryDelay):
			}
		}
		for _, reg := range s.registrar.All() {
			remote := contactAddr(reg.Contact)
			if remote == nil {
				continue
			}
			// The SDP c= line and Contact must advertise an address the
			// client can actually reach; derive it from the socket we use to
			// reach that very client.
			local := s.localIPFor(remote)
			if reachable == "" {
				reachable = local
			}
			inviteSDP := fmt.Sprintf("v=0\r\no=cellbridge 0 0 IN IP4 %s\r\ns=CellBridge\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n", local, local, media.LocalAddr().Port)
			invite := fmt.Sprintf("INVITE sip:%s@%s SIP/2.0\r\nVia: SIP/2.0/UDP %s:5060;branch=z9hG4bK%s;rport\r\nFrom: <sip:%s@%s>;tag=cb%s\r\nTo: <sip:%s@%s>\r\nCall-ID: %s\r\nCSeq: 1 INVITE\r\nContact: <sip:cellbridge@%s:5060>\r\nMax-Forwards: 70\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s", reg.Username, local, local, callID[len("in-"):][:8], peer, local, callID[len("in-"):][:8], reg.Username, local, callID, local, len(inviteSDP), inviteSDP)
			if _, err := s.conn.WriteToUDP([]byte(invite), remote); err != nil {
				slog.Warn("sip inbound invite failed", "user", reg.Username, "err", err)
				continue
			}
			sent++
			slog.Info("sip inbound invite sent", "user", reg.Username, "contact", remote.String(), "call", callID, "peer", peer, "attempt", attempt+1)
		}
	}
	if sent == 0 {
		slog.Warn("sip inbound invite unsent", "call", callID, "peer", peer, "reason", "no registered client reachable")
	}
	if reachable == "" {
		reachable = s.outboundIP()
	}
	s.sendYakPush("sip:"+peer+"@"+reachable, "voip", "")
	slog.Info("sip inbound ringing", "call_id", callID, "peer", peer, "invites_sent", sent, "push_host", reachable)
	// Nobody picked up: release the cellular leg so the modem is not left
	// ringing forever (which would make every later dial fail with
	// ErrActiveCall until the gateway restarted).
	go func() {
		select {
		case <-time.After(inboundRingTimeout):
			if _, ok := s.sessions.Load(callID); ok {
				slog.Info("sip inbound ring timeout", "call", callID)
				s.sessions.Delete(callID)
				_ = sess.Hangup()
			}
		case <-sess.ctx.Done():
		}
	}()
}

// inboundRingTimeout bounds how long an inbound call may ring before the
// gateway gives up and hangs up the cellular leg.
const inboundRingTimeout = 45 * time.Second

// YakPhone re-registers extremely aggressively — an expires=0 unregister
// immediately followed by a fresh register, several times a minute — so the
// registrar can be momentarily empty at the exact instant a call arrives.
// A single delivery attempt then finds no target and the call is dropped on
// the floor. Retry briefly before giving up; the retry only runs while
// nothing has been delivered, so a client can never receive two INVITEs for
// the same call.
const (
	inboundInviteAttempts   = 10
	inboundInviteRetryDelay = 500 * time.Millisecond
)

// contactAddr extracts host:port from a SIP Contact header value.
func contactAddr(contact string) *net.UDPAddr {
	if i := strings.Index(contact, "sip:"); i >= 0 {
		rest := contact[i+4:]
		if j := strings.IndexAny(rest, ">;"); j >= 0 {
			rest = rest[:j]
		}
		// A Contact is user@host:port, but ResolveUDPAddr only accepts
		// host:port. The upstream code never stripped the user part, so
		// resolution always failed, contactAddr returned nil and every
		// inbound INVITE was skipped by the `if remote == nil { continue }`
		// guard below — the SIP client never rang on an incoming call.
		if at := strings.LastIndex(rest, "@"); at >= 0 {
			rest = rest[at+1:]
		}
		if addr, err := net.ResolveUDPAddr("udp", rest); err == nil {
			return addr
		}
	}
	return nil
}

// localIPFor returns the local address whose route reaches remote. The
// upstream nasIP() only looked for a tailnet (100.x) address and otherwise
// fell back to 127.0.0.1; a plain LAN client then received SDP with
// "c=IN IP4 127.0.0.1" and sent its RTP to itself, so the uplink was silent
// even though the downlink worked.
func (s *Server) localIPFor(remote *net.UDPAddr) string {
	if remote != nil && !remote.IP.IsUnspecified() {
		if conn, err := net.DialUDP("udp", nil, remote); err == nil {
			defer conn.Close()
			if la, ok := conn.LocalAddr().(*net.UDPAddr); ok && la.IP != nil && !la.IP.IsUnspecified() {
				return la.IP.String()
			}
		}
	}
	if ip := localTailnetIP(); ip != "" {
		return ip
	}
	return "127.0.0.1"
}

func (s *Server) handleMessage(msg string, remote *net.UDPAddr) {
	lines := strings.Split(msg, "\r\n")
	if len(lines) == 0 {
		return
	}
	first := lines[0]
	// Responses to the INVITEs the gateway itself sent out for inbound
	// cellular calls. The upstream code only parsed requests, so a client
	// picking up an inbound call was silently ignored: the phone rang,
	// the user answered, and nothing ever happened.
	if strings.HasPrefix(first, "SIP/2.0") {
		s.handleResponse(msg, remote)
		return
	}
	if strings.HasPrefix(first, "REGISTER") {
		s.handleRegister(msg, remote)
		return
	}
	if strings.HasPrefix(first, "INVITE") {
		s.handleInvite(msg, remote)
		return
	}
	if strings.HasPrefix(first, "ACK") {
		// ACK is part of the existing INVITE transaction; it is never answered.
		return
	}
	if strings.HasPrefix(first, "BYE") || strings.HasPrefix(first, "CANCEL") {
		s.handleAckBye(msg, remote, first)
		return
	}
	if strings.HasPrefix(first, "OPTIONS") {
		s.sendResponse(remote, msg, 200, "OK", "", "")
		return
	}
	if strings.HasPrefix(first, "MESSAGE") {
		s.handleMessageRequest(msg, remote)
		return
	}
}

// handleResponse processes responses to gateway-originated INVITEs, i.e.
// the client answering (or rejecting) an inbound cellular call.
func (s *Server) handleResponse(msg string, remote *net.UDPAddr) {
	lines := strings.Split(msg, "\r\n")
	if len(lines) == 0 {
		return
	}
	fields := strings.SplitN(lines[0], " ", 3)
	if len(fields) < 2 {
		return
	}
	code := 0
	fmt.Sscanf(fields[1], "%d", &code)
	callID := parseHeader(msg, "Call-ID")
	cseq := parseHeader(msg, "CSeq")
	if callID == "" {
		return
	}
	v, ok := s.sessions.Load(callID)
	if !ok {
		return
	}
	sess, ok := v.(*SIPCallSession)
	if !ok || sess.Direction != "inbound" {
		return
	}
	switch {
	case code == 100 || code == 180 || code == 183:
		// Provisional: the client is ringing. Logged deliberately — a bare
		// "phone did not ring" report is otherwise indistinguishable from
		// three very different causes. 180/183 proves YakPhone received the
		// INVITE and put the call on screen; only a 100 (or nothing at all)
		// means the app never presented it, which points at the app/OS side
		// (background suspension → needs the PushKit push) rather than at
		// the gateway.
		slog.Info("sip inbound provisional", "call", callID, "code", code)
		return
	case code == 200 && strings.Contains(strings.ToUpper(cseq), "INVITE"):
		go s.acceptInbound(sess, msg, remote)
	case code >= 300:
		slog.Warn("sip inbound call declined", "call", callID, "code", code)
		s.sessions.Delete(callID)
		_ = sess.Hangup()
	}
}

// acceptInbound finishes an inbound call the client just answered: ACK the
// 200 OK, point RTP at the client, pick up the cellular leg (ATA) and start
// the PCM<->RTP bridge.
func (s *Server) acceptInbound(sess *SIPCallSession, msg string, remote *net.UDPAddr) {
	if !sess.beginAnswer() {
		return
	}
	callID := sess.ID
	s.sendACK(msg, remote, sess.Peer)
	rtpTarget := inboundRTPTarget(remote, extractSDP(msg))
	if rtpTarget != "" {
		if err := sess.media.SetRemote(rtpTarget); err != nil {
			slog.Warn("sip inbound rtp target failed", "call", callID, "err", err)
		}
	} else {
		// MediaSession.WritePCMU silently drops every frame while the remote
		// is unset, so a missing SDP used to mean a silent call with no clue.
		slog.Warn("sip inbound answer had no usable SDP; RTP target unset", "call", callID, "remote", remote.String())
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := sess.AnswerInbound(ctx); err != nil {
		slog.Warn("sip inbound answer failed", "call", callID, "err", err)
		s.sessions.Delete(callID)
		_ = sess.Hangup()
		return
	}
	slog.Info("sip inbound connected", "call", callID, "peer", sess.Peer, "rtp_remote", rtpTarget)
}

// inboundRTPTarget picks where to send RTP for a call the client just
// answered: the media port from the client's SDP, but always the packet's
// source IP. The dial path already ignored the SDP c= line — YakPhone /
// baresip advertise a WAN or LAN address there that is unreachable from
// this host (tailnet peers talk over 100.x) — while the answer path trusted
// it and aimed RTP at an address that never answered, so the caller heard
// nothing. Returns "" when the answer carried no usable media description.
func inboundRTPTarget(remote *net.UDPAddr, sdp string) string {
	if remote == nil {
		return ""
	}
	_, port := parseSDPRTP(sdp)
	if port == 0 {
		return ""
	}
	return fmt.Sprintf("%s:%d", remote.IP.String(), port)
}

// sendACK acknowledges the 200 OK that answered one of our inbound
// INVITEs. A SIP client keeps retransmitting the 200 until it sees the
// ACK, so omitting it makes the call drop a few seconds after pickup.
func (s *Server) sendACK(resp string, remote *net.UDPAddr, peer string) {
	from := parseHeader(resp, "From")
	to := parseHeader(resp, "To")
	callID := parseHeader(resp, "Call-ID")
	cseq := parseHeader(resp, "CSeq")
	seq := "1"
	if parts := strings.Fields(cseq); len(parts) > 0 {
		seq = parts[0]
	}
	user := extractSIPUser(to)
	if user == "" {
		user = extractSIPUser(from)
	}
	ack := fmt.Sprintf("ACK sip:%s@%s SIP/2.0\r\nVia: SIP/2.0/UDP %s:5060;branch=z9hG4bK%s;rport\r\nMax-Forwards: 70\r\nFrom: %s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %s ACK\r\nContent-Length: 0\r\n\r\n", user, remote.String(), s.localIPFor(remote), uuid.NewString()[:12], from, to, callID, seq)
	if _, err := s.conn.WriteToUDP([]byte(ack), remote); err != nil {
		slog.Warn("sip ack failed", "call", callID, "err", err)
	}
}

func parseHeader(msg, name string) string {
	for _, line := range strings.Split(msg, "\r\n") {
		if strings.HasPrefix(strings.ToLower(line), strings.ToLower(name)+":") {
			return strings.TrimSpace(line[len(name)+1:])
		}
	}
	return ""
}

// handleMessageRequest delivers a SIP MESSAGE (from YakPhone, which is
// baresip-based and sends SMS as SIP instant messages) over the cellular
// modem via the attached SMS sender (§30). Responds 200 on acceptance,
// 500 when no SMS engine is wired or the modem rejects the submission.
func (s *Server) handleMessageRequest(msg string, remote *net.UDPAddr) {
	if s.sendSMS == nil {
		slog.Warn("sip message rejected", "reason", "SMS engine not attached")
		s.sendResponse(remote, msg, 500, "Server Error", "", "")
		return
	}
	// The destination is the Request-URI user part: MESSAGE sip:185xxx@host.
	requestURI := strings.Fields(msg)[1]
	destination := requestURI
	if index := strings.Index(requestURI, "@"); index > 0 {
		destination = strings.TrimPrefix(requestURI[:index], "sip:")
	}
	destination = strings.TrimLeft(destination, "+\x20")
	if destination == "" {
		slog.Warn("sip message rejected", "reason", "empty destination")
		s.sendResponse(remote, msg, 400, "Bad Request", "", "")
		return
	}
	// Message body follows the blank line (Content-Type: text/plain).
	body := ""
	if sections := strings.SplitN(msg, "\r\n\r\n", 2); len(sections) == 2 {
		body = strings.TrimSpace(sections[1])
	}
	if body == "" {
		slog.Warn("sip message rejected", "reason", "empty body", "to", destination)
		s.sendResponse(remote, msg, 400, "Bad Request", "", "")
		return
	}
	ctx, cancel := context.WithTimeout(s.ctx, 30*time.Second)
	defer cancel()
	if err := s.sendSMS(ctx, destination, body); err != nil {
		slog.Warn("sip message send failed", "to", destination, "err", err)
		s.sendResponse(remote, msg, 500, "Server Error", "", "")
		return
	}
	slog.Info("sip message sent", "to", destination, "length", len(body))
	s.sendResponse(remote, msg, 200, "OK", "", "")
}

func (s *Server) handleRegister(msg string, remote *net.UDPAddr) {
	from := parseHeader(msg, "From")
	to := parseHeader(msg, "To")
	contact := parseHeader(msg, "Contact")
	expiresStr := parseHeader(msg, "Expires")
	username := extractSIPUser(to)
	if username == "" {
		username = extractSIPUser(from)
	}
	if s.auth.NeedsAuth(msg) {
		hdrs := "WWW-Authenticate: " + WWWAuthHeader(s.auth.Realm(), s.auth.Nonce()) + "\r\n"
		s.sendResponse(remote, msg, 401, "Unauthorized", hdrs, "")
		return
	}
	expires := 3600
	if expiresStr != "" {
		fmt.Sscanf(expiresStr, "%d", &expires)
	}
	if strings.Contains(contact, "expires=0") {
		expires = 0
	}
	s.registrar.Register(username, contact, "UDP", expires)
	s.sendResponse(remote, msg, 200, "OK", "Contact: "+contact+"\r\nExpires: "+fmt.Sprintf("%d", expires)+"\r\n", "")
	slog.Info("sip register", "user", username, "contact", contact, "expires", expires)
}

// reapStaleSessions hangs up and removes every lingering call session.
// Called before each outbound dial so a vanished client (no BYE ever
// arrives) cannot permanently pin the single QDC507 audio device.
func (s *Server) reapStaleSessions() {
	var stale []string
	s.sessions.Range(func(key, value any) bool {
		stale = append(stale, key.(string))
		return true
	})
	for _, id := range stale {
		if sess, ok := s.sessions.Load(id); ok {
			slog.Warn("reaping stale call session", "call", id)
			_ = sess.(*SIPCallSession).Hangup()
			s.sessions.Delete(id)
		}
	}
}

// handleInvite implements §20: invite -> 100 -> modem dial -> 180 -> wait
// cellular answer (PCM RUNNING) -> 200 OK. Retransmissions of the same
// Call-ID answer with current state, never a second dial.
func (s *Server) handleInvite(msg string, remote *net.UDPAddr) {
	from := parseHeader(msg, "From")
	to := parseHeader(msg, "To")
	peer := extractSIPUser(to)
	if peer == "" {
		peer = "unknown"
	}
	username := extractSIPUser(from)
	if _, ok := s.registrar.Get(username); !ok {
		s.sendResponse(remote, msg, 403, "Forbidden", "", "")
		return
	}
	callID := parseHeader(msg, "Call-ID")
	if callID == "" {
		callID = uuid.NewString()
	}
	if sess, ok := s.sessions.Load(callID); ok {
		existing := sess.(*SIPCallSession)
		hdrs := "Contact: <sip:cellbridge@" + s.localIPFor(remote) + ":5060>\r\nAllow: INVITE, ACK, BYE, CANCEL, OPTIONS\r\nContent-Type: application/sdp\r\n"
		if rx, tx := existing.media.Stats(); rx > 0 || tx > 0 || existing.State() == "active" {
			sdp := fmt.Sprintf("v=0\r\no=cellbridge 0 0 IN IP4 %s\r\ns=CellBridge\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n", s.localIPFor(remote), s.localIPFor(remote), existing.media.LocalAddr().Port)
			s.sendResponse(remote, msg, 200, "OK", hdrs, sdp)
		} else {
			s.sendResponse(remote, msg, 180, "Ringing", "Contact: <sip:cellbridge@"+s.localIPFor(remote)+":5060>\r\n", "")
		}
		return
	}
	media, err := NewMediaSession("0.0.0.0:0")
	if err != nil {
		s.sendResponse(remote, msg, 500, "Server Error", "", "")
		return
	}
	clientSDP := extractSDP(msg)
	rtpIP, rtpPort := parseSDPRTP(clientSDP)
	// YakPhone/baresip may advertise its public WAN address in the SDP c= line;
	// inside the tailnet that address is unreachable. Always use the INVITE
	// packet's source IP with the SDP media port.
	if rtpPort != 0 {
		_ = media.SetRemote(fmt.Sprintf("%s:%d", remote.IP.String(), rtpPort))
	}

	s.sendResponse(remote, msg, 100, "Trying", "", "")
	// Reap any stale sessions BEFORE dialing. A client that vanished
	// (app killed, network drop) never sends BYE, so its bridge keeps
	// owning the QDC507 audio device forever and every later call dies
	// at bridge.Start with "QDC507 audio is already active for call…".
	// Observed 2026-09-06: a probe script that sent INVITE without BYE
	// blocked all subsequent calls.
	s.reapStaleSessions()
	sess := NewSIPCallSession(callID, peer, "outbound", s.modem, s.audio, media)
	// Send 180 Ringing BEFORE dialing: the modem dial path includes a
	// per-call QDC507 route rotation (2-9s) + ATD. YakPhone shows the
	// caller "ringing" only after it receives 180, so a late 180 made
	// every call feel like "waits forever before ringing" after a NAS
	// reboot (observed 2026-09-06: dialing→180 gap ~9s on first call).
	// 180 is provisional and carries no SDP, so it is safe to send
	// before the cellular leg is ready.
	s.sendResponse(remote, msg, 180, "Ringing", "Contact: <sip:cellbridge@"+s.localIPFor(remote)+":5060>\r\n", "")
	if err := sess.Dial(); err != nil {
		slog.Warn("sip invite dial failed", "err", err)
		s.sendResponse(remote, msg, 500, "Server Error", "", "")
		s.sessions.Delete(callID)
		_ = media.Close()
		return
	}
	s.sessions.Store(callID, sess)
	go func() {
		answerCtx, cancel := context.WithTimeout(context.Background(), answerTimeout)
		defer cancel()
		if err := sess.AwaitBridge(answerCtx); err != nil {
			slog.Warn("sip await bridge failed", "call", callID, "err", err)
			_ = sess.Hangup()
			s.sessions.Delete(callID)
			return
		}
		sdp := fmt.Sprintf("v=0\r\no=cellbridge 0 0 IN IP4 %s\r\ns=CellBridge\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n", s.localIPFor(remote), s.localIPFor(remote), media.LocalAddr().Port)
		hdrs := "Contact: <sip:cellbridge@" + s.localIPFor(remote) + ":5060>\r\nAllow: INVITE, ACK, BYE, CANCEL, OPTIONS\r\nContent-Type: application/sdp\r\n"
		s.sendResponse(remote, msg, 200, "OK", hdrs, sdp)
		slog.Info("sip invite handled", "call", callID, "peer", peer, "rtp_remote", fmt.Sprintf("%s:%d", rtpIP, rtpPort))
	}()
}

func (s *Server) handleAckBye(msg string, remote *net.UDPAddr, first string) {
	callID := parseHeader(msg, "Call-ID")
	if callID == "" {
		s.sendResponse(remote, msg, 200, "OK", "", "")
		return
	}
	if v, ok := s.sessions.Load(callID); ok {
		sess := v.(*SIPCallSession)
		if strings.HasPrefix(first, "BYE") || strings.HasPrefix(first, "CANCEL") {
			// method/state/reason together tell apart the three teardown
			// causes: "CANCEL + state=init" = the phone aborted before
			// answering (declined, or it gave up while we were still
			// dialling the cellular leg), "BYE + state=active" = a normal
			// hangup after a connected call.
			slog.Info("sip bye received",
				"call", callID,
				"method", strings.Fields(first)[0],
				"state", sess.State(),
				"reason", parseHeader(msg, "Reason"))
			_ = sess.Hangup()
			s.sessions.Delete(callID)
		}
	}
	s.sendResponse(remote, msg, 200, "OK", "", "")
}

func (s *Server) nasIP() string {
	if ip := localTailnetIP(); ip != "" {
		return ip
	}
	return "127.0.0.1"
}

// outboundIP is the fallback reachable address when no SIP client is
// registered, so localIPFor has no peer to probe. This path matters: a
// suspended YakPhone eventually loses its registration, and then the VoIP push
// is the only way to reach it — with a useless host in caller_uri the CallKit
// call cannot be answered.
func (s *Server) outboundIP() string {
	if ip := localTailnetIP(); ip != "" {
		return ip
	}
	// Prefer a real private LAN address. A bare UDP dial can land on a
	// proxy/tunnel interface (observed: utun9 = 198.18.0.1 from a TUN-based
	// proxy), which the phone cannot reach.
	if ip := privateLANIP(); ip != "" {
		return ip
	}
	conn, err := net.Dial("udp", "8.8.8.8:53")
	if err != nil {
		return s.nasIP()
	}
	defer conn.Close()
	if addr, ok := conn.LocalAddr().(*net.UDPAddr); ok && addr.IP != nil && !addr.IP.IsUnspecified() {
		return addr.IP.String()
	}
	return s.nasIP()
}

// privateLANIP returns the first private IPv4 on an up, non-tunnel interface.
func privateLANIP() string {
	interfaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		name := iface.Name
		if strings.HasPrefix(name, "utun") || strings.HasPrefix(name, "tun") ||
			strings.HasPrefix(name, "tap") || strings.HasPrefix(name, "awdl") ||
			strings.HasPrefix(name, "llw") {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip4 := ipNet.IP.To4()
			if ip4 == nil || !ip4.IsPrivate() {
				continue
			}
			return ip4.String()
		}
	}
	return ""
}

func (s *Server) sendResponse(remote *net.UDPAddr, req string, code int, reason, extraHeaders, body string) {
	callID := parseHeader(req, "Call-ID")
	from := parseHeader(req, "From")
	to := parseHeader(req, "To")
	via := parseHeader(req, "Via")
	cseq := parseHeader(req, "CSeq")
	tag := ";tag=" + uuid.NewString()[:8]
	if strings.Contains(to, "tag=") {
		tag = ""
	}
	resp := fmt.Sprintf("SIP/2.0 %d %s\r\nVia: %s\r\nFrom: %s\r\nTo: %s%s\r\nCall-ID: %s\r\nCSeq: %s\r\n%sContent-Length: %d\r\n\r\n%s", code, reason, via, from, to, tag, callID, cseq, extraHeaders, len(body), body)
	_, _ = s.conn.WriteToUDP([]byte(resp), remote)
}

func extractSIPUser(hdr string) string {
	s := hdr
	if i := strings.Index(s, "sip:"); i >= 0 {
		s = s[i+4:]
	} else {
		return ""
	}
	if j := strings.Index(s, "@"); j >= 0 {
		return s[:j]
	}
	if j := strings.Index(s, ">"); j >= 0 {
		return s[:j]
	}
	return strings.Fields(s)[0]
}
func extractSDP(msg string) string {
	parts := strings.Split(msg, "\r\n\r\n")
	if len(parts) < 2 {
		return ""
	}
	return parts[1]
}
func parseSDPRTP(sdp string) (string, int) {
	var ip string
	var port int
	for _, line := range strings.Split(sdp, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "c=IN IP4 ") {
			ip = strings.TrimSpace(line[len("c=IN IP4 "):])
		}
		if strings.HasPrefix(line, "m=audio ") {
			fmt.Sscanf(line, "m=audio %d", &port)
		}
	}
	return ip, port
}

// answerTimeout bounds how long we wait for the cellular leg to be
// answered before giving up on the SIP call.
const answerTimeout = 90 * time.Second

// localTailnetIP returns the NAS tailnet IPv4 address by walking its
// interfaces, preferring tailscale0.
func localTailnetIP() string {
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, a := range addrs {
			if ipnet, ok := a.(*net.IPNet); ok && !ipnet.IP.IsLoopback() && ipnet.IP.To4() != nil {
				if strings.HasPrefix(ipnet.IP.String(), "100.") {
					return ipnet.IP.String()
				}
			}
		}
	}
	return ""
}
