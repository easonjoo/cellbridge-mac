// voice-audio-bridge — DJiPhone Kit 通话音频桥（macOS）
// 方向 1：模块 UAC "AC Interface"(8kHz, 蜂窝→Mac)  → Mac 默认输出（扬声器）
// 方向 2：Mac 默认输入（麦克风）→ 模块 UAC "AS Interface"(8kHz, Mac→蜂窝)
//
// 用法：voice-audio-bridge [--in-name AC] [--out-name AS] [--verbose]
// 退出：SIGTERM/SIGINT 时干净停止。

import Foundation
import CoreAudio
import AudioToolbox

// MARK: - 环形缓冲（单写单读，锁保护足够）
final class RingBuffer {
    private var buf: [Float]
    private var r = 0
    private var w = 0
    private let lock = NSLock()
    private let capacity: Int

    init(capacity: Int = 1 << 16) {
        self.capacity = capacity
        self.buf = [Float](repeating: 0, count: capacity)
    }

    func write(_ data: UnsafePointer<Float>, count n: Int) {
        lock.lock()
        for i in 0..<n {
            let next = (w + 1) % capacity
            if next == r { r = (r + 1) % capacity }  // 满则丢最旧
            buf[w] = data[i]
            w = next
        }
        lock.unlock()
    }

    func read(_ out: UnsafeMutablePointer<Float>, count n: Int) -> Int {
        lock.lock()
        var got = 0
        while got < n && r != w {
            out[got] = buf[r]
            r = (r + 1) % capacity
            got += 1
        }
        lock.unlock()
        return got
    }
}

// MARK: - CoreAudio 工具
func getDevices() -> [(id: AudioDeviceID, name: String)] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMaster)
    var size: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    guard status == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    guard status == noErr else { return [] }
    var result: [(AudioDeviceID, String)] = []
    for id in ids {
        var nameAddr = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMaster)
        var cfName: CFString? = nil
        var nameSize = UInt32(MemoryLayout<CFString?>.size)
        if AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &nameSize, &cfName) == noErr,
           let n = cfName as String? {
            result.append((id, n))
        }
    }
    return result
}

func findDevice(matching needle: String) -> AudioDeviceID? {
    for (id, name) in getDevices() where name.localizedCaseInsensitiveContains(needle) {
        return id
    }
    return nil
}

func defaultDevice(_ scope: AudioObjectPropertyScope) -> AudioDeviceID? {
    var addr = AudioObjectPropertyAddress(
        mSelector: scope == kAudioObjectPropertyScopeOutput
            ? kAudioHardwarePropertyDefaultOutputDevice
            : kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMaster)
    var id = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id) == noErr, id != 0 else {
        return nil
    }
    return id
}

let SAMPLE_RATE: Float64 = 8000

func makeFormat() -> AudioStreamBasicDescription {
    AudioStreamBasicDescription(
        mSampleRate: SAMPLE_RATE,
        mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        mBytesPerPacket: 4, mFramesPerPacket: 1,
        mBytesPerFrame: 4, mChannelsPerFrame: 1,
        mBitsPerChannel: 32, mReserved: 0)
}

// HAL 单元：一个 AUHAL 同时配输入设备与输出方向。direction=false 表示输入捕获。
func makeHALUnit(device: AudioDeviceID, enableInput: Bool, enableOutput: Bool) -> AudioUnit? {
    var au: AudioUnit?
    var comp = AudioComponentDescription(
        componentType: kAudioUnitType_Output,
        componentSubType: kAudioUnitSubType_HALOutput,
        componentManufacturer: kAudioUnitManufacturer_Apple,
        componentFlags: 0, componentFlagsMask: 0)
    guard let compRef = AudioComponentFindNext(nil, &comp) else { return nil }
    guard AudioComponentInstanceNew(compRef, &au) == noErr, let au = au else { return nil }

    var one: UInt32 = 1, zero: UInt32 = 0
    // element 1 = input side, element 0 = output side
    AudioUnitSetProperty(au, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &one, UInt32(MemoryLayout<UInt32>.size))
    AudioUnitSetProperty(au, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &zero, UInt32(MemoryLayout<UInt32>.size))
    if enableOutput {
        AudioUnitSetProperty(au, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &one, UInt32(MemoryLayout<UInt32>.size))
    }
    if !enableInput {
        AudioUnitSetProperty(au, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &zero, UInt32(MemoryLayout<UInt32>.size))
    }

    var dev = device
    guard AudioUnitSetProperty(au, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &dev, UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr else {
        AudioComponentInstanceDispose(au)
        return nil
    }
    return au
}

func setClientFormat(_ au: AudioUnit, _ fmt: UnsafePointer<AudioStreamBasicDescription>) -> Bool {
    return AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, fmt, UInt32(MemoryLayout<AudioStreamBasicDescription>.size)) == noErr
}

func setInputFormat(_ au: AudioUnit, _ fmt: UnsafePointer<AudioStreamBasicDescription>) -> Bool {
    return AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, fmt, UInt32(MemoryLayout<AudioStreamBasicDescription>.size)) == noErr
}

// MARK: - 桥接通道
final class Channel {
    let ring = RingBuffer()
    var inputUnit: AudioUnit?
    var outputUnit: AudioUnit?
    let label: String
    var verbose = false
    var totalFrames: UInt64 = 0

    init(label: String) { self.label = label }
}

func inputCallback(_ inRefCon: UnsafeMutableRawPointer, _ ioActionFlags: UnsafeMutablePointer<AudioUnitRenderActionFlags>, _ inTimeStamp: UnsafePointer<AudioTimeStamp>, _ inBusNumber: UInt32, _ inNumberFrames: UInt32, _ ioData: UnsafeMutablePointer<AudioBufferList>?) -> OSStatus {
    let ch = Unmanaged<Channel>.fromOpaque(inRefCon).takeUnretainedValue()
    guard let ablPtr = ioData else { return -1 }
    var status = AudioUnitRender(ch.inputUnit!, ioActionFlags, inTimeStamp, inBusNumber, inNumberFrames, ablPtr)
    guard status == noErr else { return status }
    let bufPtr = ablPtr.pointee.mBuffers
    if bufPtr.mData != nil, bufPtr.mNumberChannels == 1 || bufPtr.mNumberChannels >= 1 {
        let floats = bufPtr.mData!.assumingMemoryBound(to: Float.self)
        let frames = Int(inNumberFrames)
        ch.ring.write(floats, count: frames)
        ch.totalFrames += UInt64(frames)
    }
    return noErr
}

func renderCallback(_ inRefCon: UnsafeMutableRawPointer, _ ioActionFlags: UnsafeMutablePointer<AudioUnitRenderActionFlags>, _ inTimeStamp: UnsafePointer<AudioTimeStamp>, _ inBusNumber: UInt32, _ inNumberFrames: UInt32, _ ioData: UnsafeMutablePointer<AudioBufferList>?) -> OSStatus {
    let ch = Unmanaged<Channel>.fromOpaque(inRefCon).takeUnretainedValue()
    guard let ablPtr = ioData else { return -1 }
    let bufPtr = ablPtr.pointee.mBuffers
    guard let data = bufPtr.mData else { return -1 }
    let floats = data.assumingMemoryBound(to: Float.self)
    let frames = Int(inNumberFrames)
    let got = ch.ring.read(floats, count: frames)
    if got < frames {
        for i in got..<frames { floats[i] = 0 }  // 欠载补零
    }
    return noErr
}

func installCallback(_ au: AudioUnit, _ ch: Channel, isInput: Bool) -> Bool {
    let proc: @convention(c) (UnsafeMutableRawPointer, UnsafeMutablePointer<AudioUnitRenderActionFlags>, UnsafePointer<AudioTimeStamp>, UInt32, UInt32, UnsafeMutablePointer<AudioBufferList>?) -> OSStatus
        = isInput ? inputCallback : renderCallback
    var cb = AURenderCallbackStruct(
        inputProc: proc,
        inputProcRefCon: Unmanaged.passUnretained(ch).toOpaque())
    if isInput {
        return AudioUnitSetProperty(au, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0, &cb, UInt32(MemoryLayout<AURenderCallbackStruct>.size)) == noErr
    }
    return AudioUnitSetProperty(au, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &cb, UInt32(MemoryLayout<AURenderCallbackStruct>.size)) == noErr
}

// MARK: - 主流程
var interrupted = false
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: DispatchQueue.global())
    src.setEventHandler { interrupted = true }
    src.resume()
}

var args = Array(CommandLine.arguments.dropFirst())
var verbose = args.contains("--verbose")
func argValue(_ flag: String) -> String? {
    if let i = args.firstIndex(of: flag), i + 1 < args.count { return args[i + 1] }
    return nil
}
let cellularInName = argValue("--in-name") ?? "AC Interface"
let cellularOutName = argValue("--out-name") ?? "AS Interface"

guard let acDevice = findDevice(matching: cellularInName) else {
    FileHandle.standardError.write("找不到模块 UAC 输入设备（含 \(cellularInName)）\n".data(using: .utf8)!)
    exit(2)
}
guard let asDevice = findDevice(matching: cellularOutName) else {
    FileHandle.standardError.write("找不到模块 UAC 输出设备（含 \(cellularOutName)）\n".data(using: .utf8)!)
    exit(2)
}
guard let macOut = defaultDevice(kAudioObjectPropertyScopeOutput) else {
    FileHandle.standardError.write("找不到 Mac 默认输出设备\n".data(using: .utf8)!)
    exit(2)
}
guard let macIn = defaultDevice(kAudioObjectPropertyScopeInput) else {
    FileHandle.standardError.write("找不到 Mac 默认输入设备\n".data(using: .utf8)!)
    exit(2)
}

let down = Channel(label: "cellular->mac")   // AC 蜂窝进 Mac 扬声器
let up = Channel(label: "mac->cellular")     // 麦克风进 AS 蜂窝
down.verbose = verbose; up.verbose = verbose

var fmt = makeFormat()

// 方向 1：输入=AC 设备（蜂窝），输出=Mac 扬声器
down.inputUnit = makeHALUnit(device: acDevice, enableInput: true, enableOutput: false)
down.outputUnit = makeHALUnit(device: macOut, enableInput: false, enableOutput: true)
// 方向 2：输入=Mac 麦克风，输出=AS 设备（蜂窝）
up.inputUnit = makeHALUnit(device: macIn, enableInput: true, enableOutput: false)
up.outputUnit = makeHALUnit(device: asDevice, enableInput: false, enableOutput: true)

for ch in [down, up] {
    guard let iu = ch.inputUnit, let ou = ch.outputUnit else {
        FileHandle.standardError.write("AudioUnit 创建失败（\(ch.label)）\n".data(using: .utf8)!)
        exit(3)
    }
    guard setClientFormat(iu, &fmt), setInputFormat(iu, &fmt),
          setClientFormat(ou, &fmt),
          installCallback(iu, ch, isInput: true),
          installCallback(ou, ch, isInput: false),
          AudioUnitInitialize(iu) == noErr,
          AudioUnitInitialize(ou) == noErr else {
        FileHandle.standardError.write("AudioUnit 配置失败（\(ch.label)）\n".data(using: .utf8)!)
        exit(3)
    }
    guard AudioOutputUnitStart(iu) == noErr, AudioOutputUnitStart(ou) == noErr else {
        FileHandle.standardError.write("AudioUnit 启动失败（\(ch.label)）\n".data(using: .utf8)!)
        exit(3)
    }
}

FileHandle.standardError.write("voice-audio-bridge 运行中：\(cellularInName)→扬声器，麦克风→\(cellularOutName)\n".data(using: .utf8)!)

// 心跳：每 5 秒打印统计
var lastDown: UInt64 = 0, lastUp: UInt64 = 0
while !interrupted {
    Thread.sleep(forTimeInterval: 5)
    let d = down.totalFrames, u = up.totalFrames
    if verbose {
        FileHandle.standardError.write(String(format: "[stats] down=%llu fr (%llu/s) up=%llu fr (%llu/s)\n", d - lastDown, (d - lastDown) / 5, u - lastUp, (u - lastUp) / 5).data(using: .utf8)!)
    }
    lastDown = d; lastUp = u
}

for ch in [down, up] {
    if let iu = ch.inputUnit { AudioOutputUnitStop(iu); AudioUnitUninitialize(iu) }
    if let ou = ch.outputUnit { AudioOutputUnitStop(ou); AudioUnitUninitialize(ou) }
}
FileHandle.standardError.write("voice-audio-bridge 已退出\n".data(using: .utf8)!)
