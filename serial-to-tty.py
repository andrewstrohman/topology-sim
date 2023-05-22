#!/usr/bin/env python3

import json
import os
import sys


USB_SERIAL_PATH = "/sys/bus/usb-serial/devices"

def get_usb_serial_devices():
    return os.listdir(USB_SERIAL_PATH)

def get_serial_to_tty():
    ret = {}
    for dir_path, dirs, files in os.walk("/sys/devices"):
        for file_name in files:
            if file_name == "serial":
                with open(os.path.join(dir_path, "serial")) as file_handle:
                    serial = file_handle.read().strip()
                for directory in dirs:
                    if directory.startswith(os.path.basename(dir_path) + ":"):
                        for dir_entry in os.listdir(os.path.join(dir_path, directory)):
                                if dir_entry.startswith("tty"):
                                    ret[serial] = dir_entry
    return ret

serial_to_tty = get_serial_to_tty()

if len(sys.argv) == 1:
    print(json.dumps(serial_to_tty, indent=4))
elif sys.argv[1] == "tty":
    tty = sys.argv[2]
    if tty in get_usb_serial_devices():
        print(tty, end="")
elif sys.argv[1] == "serial":
    serial = sys.argv[2]
    if serial in serial_to_tty:
        print(serial_to_tty[serial], end="")
