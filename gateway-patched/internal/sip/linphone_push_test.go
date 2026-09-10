package sip

// Linphone 推送参数解析与 Call-ID 清洗的回归测试
// （2026-09-11 引入 FlexiAPI 推送，wiki 示例的两种 Contact 形式都要认）。

import "testing"

func TestParsePushParamsRFC8599(t *testing.T) {
	// Linphone Android 5.3 wiki 示例（真实形态，iOS 同构，provider=apns）
	contact := `<sip:xxxx@192.168.0.1:48262;pn-prid=fCS-_c62SWmskRTsmm7Duc:VAV91bG5ePF60;pn-provider=apns;pn-param=ABCD1234.org.linphone.phone.voip;pn-silent=1;transport=tls>`
	pp, ok := parsePushParams(contact)
	if !ok {
		t.Fatal("expected push params to be found")
	}
	if pp.Provider != "apns" {
		t.Errorf("provider = %q, want apns", pp.Provider)
	}
	if pp.Param != "ABCD1234.org.linphone.phone.voip" {
		t.Errorf("param = %q", pp.Param)
	}
	if pp.Prid != "fCS-_c62SWmskRTsmm7Duc:VAV91bG5ePF60" {
		t.Errorf("prid = %q", pp.Prid)
	}
}

func TestParsePushParamsLegacy(t *testing.T) {
	// 旧式 Flexisip 参数（pn-type/pn-tok/app-id）
	contact := `<sip:u@1.2.3.4:1234;app-id=org.linphone.phone;pn-type=apple;pn-tok=ABA3D8A75E1A4F2C;transport=udp>`
	pp, ok := parsePushParams(contact)
	if !ok {
		t.Fatal("expected legacy push params to be found")
	}
	if pp.Provider != "apns" || pp.Prid != "ABA3D8A75E1A4F2C" || pp.Param != "org.linphone.phone" {
		t.Errorf("legacy parse = %+v", pp)
	}
}

func TestParsePushParamsAbsent(t *testing.T) {
	// YakPhone / 普通 SIP 客户端：没有 pn-*，绝不能误报
	contact := `<sip:iphone@192.168.31.14:5060>`
	if _, ok := parsePushParams(contact); ok {
		t.Fatal("expected no push params")
	}
	// 只有部分参数也不算
	if _, ok := parsePushParams(`<sip:u@h;pn-provider=apns>`); ok {
		t.Fatal("partial params must not count")
	}
}

func TestSanitizeCallID(t *testing.T) {
	// 我们自己的入呼 Call-ID 必须原样保留（INVITE 与推送要一致）
	for _, id := range []string{"in-7b44fd354a987dda", "in-1a2b3c4d5e6f", "in-abc-DEF-123"} {
		if got := sanitizeCallID(id); got != id {
			t.Errorf("sanitizeCallID(%q) = %q, want unchanged", id, got)
		}
	}
	if got := sanitizeCallID("in-a@b.c"); got != "in-a-b-c" {
		t.Errorf("sanitizeCallID at-sign = %q", got)
	}
}
