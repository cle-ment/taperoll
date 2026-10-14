# Replace sounddevice before taperoll is imported, so tests run
# without audio hardware (or PortAudio) against a fake 8-input interface.
import sys

from tests import fake_sounddevice

sys.modules["sounddevice"] = fake_sounddevice
