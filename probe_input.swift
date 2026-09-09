// probe_input.swift — 独立诊断：AC Interface 输入回调是否触发、AudioUnitRender 是否成功
import Foundation
import CoreAudio
import AudioToolbox

func getDevices() -> [(id: AudioDeviceID, name: String)] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr else { return [] }
    var result: [(AudioDeviceID, String)] = []
    for id in ids {
        var nameAddr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var cf: CFString? = nil
        var ns = UInt32(MemoryLayout<CFString?>.size)
        if AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &ns, &cf) == noErr, let n = cf as String? { result.append((id, n)) }
    }
    return result
}

guard let dev = getDevices().first(where: { $0.name.localizedCaseInsensitiveContains("AC Interface") }) else {
    print("找不到 AC Interface"); exit(2)
}
print("AC Interface device id=\(dev.id)")

// 查询设备原生输入 ASBD（含格式标志）
var sfAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamFormat, mScope: kAudioObjectPropertyScopeInput, mElement: 1)
var asbd = AudioStreamBasicDescription()
var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
if AudioObjectGetPropertyData(dev.id, &sfAddr, 0, nil, &size, &asbd) == noErr {
    print(String(format: "原生输入: rate=%.0f ch=%u bits=%u fmtFlags=0x%x fmtID=0x%x", asbd.mSampleRate, asbd.mChannelsPerFrame, asbd.mBitsPerChannel, asbd.mFormatFlags, asbd.mFormatID))
}
// 设备是否运行
var running: UInt32 = 0
var rsize = UInt32(MemoryLayout<UInt32>.size)
var runAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceIsRunning, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
if AudioObjectGetPropertyData(dev.id, &runAddr, 0, nil, &rsize, &running) == noErr {
    print("设备 IsRunning=\(running)")
}

var comp = AudioComponentDescription(componentType: kAudioUnitType_Output, componentSubType: kAudioUnitSubType_HALOutput, componentManufacturer: kAudioUnitManufacturer_Apple, componentFlags: 0, componentFlagsMask: 0)
guard let cref = AudioComponentFindNext(nil, &comp) else { print("找不到 AUHAL 组件"); exit(3) }
var auOut: AudioUnit? = nil
guard AudioComponentInstanceNew(cref, &auOut) == noErr, let unit = auOut else {
    print("AU 创建失败"); exit(3)
}
var one: UInt32 = 1, zero: UInt32 = 0
AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &one, 4)
AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &zero, 4)
var d = dev.id
print("设置设备: \(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &d, UInt32(MemoryLayout<AudioDeviceID>.size)))")

// 客户端格式：默认设备原生 float32；传参 "s16" 时用 s16 测试
var fmt = asbd
if CommandLine.arguments.contains("s16") {
    fmt = AudioStreamBasicDescription(mSampleRate: 8000, mFormatID: kAudioFormatLinearPCM, mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked, mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2, mChannelsPerFrame: 1, mBitsPerChannel: 16, mReserved: 0)
    print("使用 s16 客户端格式")
}
print("设置客户端格式(elem1/out): \(AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &fmt, UInt32(MemoryLayout<AudioStreamBasicDescription>.size)))")
if CommandLine.arguments.contains("elem0") {
    var fmt2 = fmt
    print("额外设置 elem0/in: \(AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &fmt2, UInt32(MemoryLayout<AudioStreamBasicDescription>.size)))")
}

final class Ctx { var n = 0; var fails = 0; var unit: AudioUnit?; var lastStatus: OSStatus = 0 }
let ctx = Ctx(); ctx.unit = unit
let cb: @convention(c) (UnsafeMutableRawPointer, UnsafeMutablePointer<AudioUnitRenderActionFlags>, UnsafePointer<AudioTimeStamp>, UInt32, UInt32, UnsafeMutablePointer<AudioBufferList>?) -> OSStatus = { ref, flags, ts, bus, frames, _ in
    let c = Unmanaged<Ctx>.fromOpaque(ref).takeUnretainedValue()
    let abl = UnsafeMutablePointer<AudioBufferList>.allocate(capacity: 1)
    defer { abl.deallocate() }
    abl.pointee.mNumberBuffers = 1
    abl.pointee.mBuffers.mNumberChannels = 1
    abl.pointee.mBuffers.mDataByteSize = UInt32(frames) * 4
    abl.pointee.mBuffers.mData = nil
    let st = AudioUnitRender(c.unit!, flags, ts, bus, frames, abl)
    c.lastStatus = st
    if st != noErr { c.fails += 1; if c.fails <= 5 { print("render 失败 status=\(st) frames=\(frames)") } }
    c.n += 1
    if c.n <= 5 || c.n % 500 == 0 { print("回调 #\(c.n) frames=\(frames) status=\(st)") }
    return noErr
}
var cbStruct = AURenderCallbackStruct(inputProc: cb, inputProcRefCon: Unmanaged.passUnretained(ctx).toOpaque())
print("安装回调: \(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0, &cbStruct, UInt32(MemoryLayout<AURenderCallbackStruct>.size)))")
print("初始化: \(AudioUnitInitialize(unit))")
print("启动: \(AudioOutputUnitStart(unit))")

for i in 1...10 {
    Thread.sleep(forTimeInterval: 1)
    print("t=\(i)s 回调次数=\(ctx.n) 失败=\(ctx.fails) lastStatus=\(ctx.lastStatus)")
}
AudioOutputUnitStop(unit); AudioUnitUninitialize(unit)
