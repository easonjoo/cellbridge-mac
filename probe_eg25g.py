#!/usr/bin/env python3
"""Probe EG25-G USB interfaces to find the AT command port."""
import usb.core
import usb.util

VENDOR_ID = 0x2CA3   # 11427 decimal
PRODUCT_ID = 0x4006  # 16390 decimal

def find_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("ERROR: EG25-G device not found")
        return None
    print(f"Found device: {dev}")
    print(f"  idVendor: 0x{dev.idVendor:04X}")
    print(f"  idProduct: 0x{dev.idProduct:04X}")
    return dev

def probe_interfaces(dev):
    cfg = dev.get_active_configuration()
    print(f"\nActive configuration: {cfg.bConfigurationValue}")
    print(f"Total interfaces: {len(cfg.interfaces())}\n")

    for iface in cfg:
        cls = iface.bInterfaceClass
        sub = iface.bInterfaceSubClass
        proto = iface.bInterfaceProtocol
        num = iface.bInterfaceNumber
        n_eps = len(ifce_endpoints(iface))

        cls_name = {
            0x02: "CDC",
            0xFF: "Vendor-specific",
            0x0A: "CDC Data",
        }.get(cls, f"Class(0x{cls:02X})")

        print(f"Interface {num}: {cls_name} (sub=0x{sub:02X}, proto=0x{proto:02X}), {n_eps} endpoints")

        for ep in iface:
            ep_addr = ep.bEndpointAddress
            ep_dir = "IN" if ep_addr & 0x80 else "OUT"
            ep_type = {0: "control", 1: "iso", 2: "bulk", 3: "interrupt"}.get(
                (ep.bmAttributes & 0x03), "?"
            )
            print(f"  EP 0x{ep_addr:02X} {ep_dir} {ep_type} maxPacket={ep.wMaxPacketSize}")

        # Try AT command on bulk endpoints
        if cls == 0xFF and n_eps >= 2:
            print(f"  -> Attempting AT command on interface {num}...")
            try_at_command(dev, iface)
        print()

def ifce_endpoints(iface):
    return list(iface)

def try_at_command(dev, iface):
    bulk_in = None
    bulk_out = None

    for ep in iface:
        ep_type = ep.bmAttributes & 0x03
        if ep_type == 2:  # bulk
            if ep.bEndpointAddress & 0x80:
                bulk_in = ep
            else:
                bulk_out = ep

    if not bulk_in or not bulk_out:
        print("  No bulk IN/OUT endpoints found, skipping")
        return

    iface_num = iface.bInterfaceNumber
    try:
        # Detach kernel driver if attached
        if dev.is_kernel_driver_active(iface_num):
            print(f"  Detaching kernel driver from interface {iface_num}")
            dev.detach_kernel_driver(iface_num)

        usb.util.claim_interface(dev, iface_num)
        print(f"  Interface {iface_num} claimed")

        # Send AT\r
        bulk_out.write(b'AT\r')
        print("  Sent: AT\\r")

        # Read response
        try:
            data = bulk_in.read(1024, timeout=3000)
            response = bytes(data).decode('utf-8', errors='replace')
            print(f"  Response: {repr(response)}")
            if 'OK' in response:
                print(f"  *** Interface {iface_num} is an AT command port! ***")
        except usb.core.USBError as e:
            print(f"  Read timeout/error: {e}")

        usb.util.release_interface(dev, iface_num)

        # Reattach kernel driver
        try:
            dev.attach_kernel_driver(iface_num)
        except Exception:
            pass

    except Exception as e:
        print(f"  Error: {e}")

if __name__ == "__main__":
    dev = find_device()
    if dev:
        probe_interfaces(dev)
