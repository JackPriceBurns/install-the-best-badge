import argparse
import ast
import binascii
import glob
import os
from pathlib import Path
import select
import sys
import time


ANIMATION = '''import math
import random
import time
import badge
from rainbowio import colorwheel

controller = badge.Badge(brightness=0.20)
pixels = controller.pixels
frame = 0
mode_frames = 200
strobe_color = (255, 255, 255)
print("BRUCON_DISC_WARP_RUNNING")

while True:
    pixels.fill((0, 0, 0))
    mode = (frame // mode_frames) & 1
    mode_frame = frame % mode_frames
    if mode == 0:
        phase = (mode_frame // 3) & 1
        for index, pixel in enumerate(badge.OUTER):
            if (index & 1) == phase:
                pixels[pixel] = colorwheel((frame * 2 + index * 21) & 255)
        for index, pixel in enumerate(badge.INNER):
            if (index & 1) == phase:
                pixels[pixel] = (20, 180, 255)
        group = (frame * 2 // 7) % 8
        for pixel in badge.DIAMONDS[group]:
            pixels[pixel] = colorwheel((frame * 6) & 255)
        for pixel in badge.DIAMONDS[(group - 1) % 8]:
            pixels[pixel] = (15, 10, 40)
        pulse = (0.5 + 0.5 * math.sin(frame * 0.11)) ** 2
        for pixel in badge.DIAMONDS[8]:
            pixels[pixel] = (int(220 * pulse), int(15 * pulse), int(180 * pulse))
    else:
        if (mode_frame // 3) % 2 == 0:
            if mode_frame % 6 == 0:
                color = colorwheel(random.randint(0, 255))
                strobe_color = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
            pixels.fill(strobe_color)
    pixels.show()
    frame += 1
    time.sleep(0.04)

'''

MARKER = b"# BRUCON_DISC_WARP_STARTUP\n"
STARTUP = MARKER + b"import disc_warp\n"


class Connection:
    def __init__(self, name):
        if os.name == "nt":
            try:
                import serial
            except ImportError as exc:
                raise RuntimeError("Install pyserial with: py -m pip install pyserial") from exc
            self.port = serial.Serial(name, 115200, timeout=0.05, write_timeout=1)
            self.fd = None
        else:
            import tty
            self.fd = os.open(name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            tty.setraw(self.fd)
            self.port = None

    def write(self, data):
        if self.port:
            self.port.write(data)
        else:
            os.write(self.fd, data)

    def read_once(self, seconds):
        if self.port:
            return self.port.read(self.port.in_waiting or 1)
        if select.select([self.fd], [], [], seconds)[0]:
            try:
                return os.read(self.fd, 8192)
            except BlockingIOError:
                pass
        return b""

    def read_for(self, seconds):
        deadline = time.monotonic() + seconds
        output = bytearray()
        while time.monotonic() < deadline:
            output.extend(self.read_once(max(0, min(0.05, deadline - time.monotonic()))))
        return bytes(output)

    def close(self):
        if self.port:
            self.port.close()
        else:
            os.close(self.fd)


def find_port(explicit):
    if explicit:
        return explicit
    if os.name == "nt":
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            raise RuntimeError("Install pyserial with: py -m pip install pyserial") from exc
        ports = [port.device for port in list_ports.comports() if port.vid == 0x2E8A]
    else:
        ports = glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*")
    if len(ports) != 1:
        raise RuntimeError(f"Expected one Pico badge serial port, found {ports}; use --port")
    return ports[0]


def run(connection, command):
    connection.write(command.encode("ascii") + b"\x04")
    output = bytearray()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        output.extend(connection.read_once(max(0, min(0.05, deadline - time.monotonic()))))
        if output.endswith(b"\x04>"):
            break
    result = bytes(output)
    if not result.startswith(b"OK") or not result.endswith(b"\x04>"):
        raise RuntimeError(f"Badge REPL response: {result!r}")
    stdout, stderr = result[2:-2].split(b"\x04", 1)
    if stderr:
        raise RuntimeError(stderr.decode(errors="replace"))
    return stdout


def read_file(connection, path):
    size = int(run(connection, f"import os; print(os.stat({path!r})[6])").strip())
    chunks = []
    for offset in range(0, size, 2048):
        statement = "import binascii; print(binascii.hexlify(open(%r,'rb').read()[%d:%d]))" % (
            path, offset, offset + 2048
        )
        chunks.append(binascii.unhexlify(ast.literal_eval(run(connection, statement).strip().decode())))
    result = b"".join(chunks)
    if len(result) != size:
        raise RuntimeError(f"Incomplete read of {path}")
    return result


def write_file(connection, path, data):
    run(connection, f"open({path!r},'wb').close()")
    for offset in range(0, len(data), 384):
        part = data[offset:offset + 384].hex()
        run(connection, "import binascii; f=open(%r,'ab'); f.write(binascii.unhexlify(%r)); f.close()" % (path, part))
    if read_file(connection, path) != data:
        raise RuntimeError(f"Verification failed for {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port")
    args = parser.parse_args()
    connection = Connection(find_port(args.port))
    try:
        print("Checking badge...", flush=True)
        connection.write(b"\x03")
        connection.read_for(0.4)
        for _ in range(3):
            connection.write(b"\x01")
            if b"raw REPL" in connection.read_for(0.4):
                break
        else:
            raise RuntimeError("Could not enter the CircuitPython raw REPL")
        run(connection, "import supervisor; supervisor.runtime.autoreload=False")
        uid = ast.literal_eval(run(connection, "import microcontroller,binascii; print(binascii.hexlify(microcontroller.cpu.uid))").strip().decode()).decode()
        layout = ast.literal_eval(run(connection, "import badge; print((hasattr(badge,'Badge'),len(badge.OUTER),len(badge.INNER),[len(group) for group in badge.DIAMONDS]))").strip().decode())
        if layout != (True, 12, 7, [2] * 8 + [3]):
            raise RuntimeError(f"Unsupported badge LED layout: {layout!r}")
        current = read_file(connection, "/code.py")
        original = read_file(connection, "/code_original.py") if current.startswith(MARKER) else current
        backup = Path.home() / ".brucon-badge-backups" / uid / "code.py"
        if backup.exists() and backup.read_bytes() != original:
            raise RuntimeError(f"Existing backup differs from badge: {backup}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        print(f"Original code saved to {backup}", flush=True)
        source = ANIMATION.encode()
        if current.startswith(MARKER):
            if read_file(connection, "/disc_warp.py") == source:
                print("Badge is already set up")
                return 0
            print("Updating animation...", flush=True)
            write_file(connection, "/disc_warp_new.py", source)
            run(connection, "import os; exec(\"if 'disc_warp_old.py' in os.listdir('/'): os.remove('/disc_warp_old.py')\")")
            run(connection, "import os; os.rename('/disc_warp.py','/disc_warp_old.py')")
            run(connection, "import os; os.rename('/disc_warp_new.py','/disc_warp.py')")
            if read_file(connection, "/disc_warp.py") != source:
                raise RuntimeError("Installed animation did not verify")
            run(connection, "import os; os.remove('/disc_warp_old.py')")
        else:
            print("Installing animation...", flush=True)
            try:
                existing = read_file(connection, "/code_original.py")
            except RuntimeError:
                existing = None
            if existing is not None and existing != original:
                raise RuntimeError("Badge's existing code_original.py differs from code.py")
            if existing is None:
                write_file(connection, "/code_original.py", original)
            write_file(connection, "/disc_warp.py", source)
            write_file(connection, "/code_warp.py", STARTUP)
            run(connection, "import os; exec(\"if 'code_stock_old.py' in os.listdir('/'): os.remove('/code_stock_old.py')\")")
            run(connection, "import os; os.rename('/code.py','/code_stock_old.py')")
            run(connection, "import os; os.rename('/code_warp.py','/code.py')")
            if read_file(connection, "/code.py") != STARTUP:
                raise RuntimeError("Installed startup did not verify")
            run(connection, "import os; os.remove('/code_stock_old.py')")
        print("Badge animation installed")
        return 0
    finally:
        connection.write(b"\x02")
        connection.read_for(0.2)
        connection.write(b"\x04")
        connection.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
