#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

import os.path
from pathlib import Path

import PIL
import pytest
from pyboy import PyBoy

from .utils import url_open

OVERWRITE_PNGS = False


# https://github.com/aaaaaa123456789/rtc3test
def test_rtc3test():
    # Has to be in here. Otherwise all test workers will import this file, and cause an error.
    rtc3test_file = "rtc3test.gb"
    if not os.path.isfile(rtc3test_file):
        print(url_open("https://pyboy.dk/mirror/LICENSE.rtc3test.txt"))
        rtc3test_data = url_open("https://pyboy.dk/mirror/rtc3test.gb")
        with open(rtc3test_file, "wb") as rom_file:
            rom_file.write(rtc3test_data)

    pyboy = PyBoy(rtc3test_file, window_type="headless")
    pyboy.set_emulation_speed(0)
    for _ in range(59):
        pyboy.tick()

    for _ in range(25):
        pyboy.tick()

    png_path = Path(f"test_results/{rtc3test_file}.png")
    image = pyboy.botsupport_manager().screen().screen_image()
    if OVERWRITE_PNGS:
        png_path.parents[0].mkdir(parents=True, exist_ok=True)
        image.save(png_path)
    else:
        old_image = PIL.Image.open(png_path)
        diff = PIL.ImageChops.difference(image, old_image)
        if diff.getbbox() and not os.environ.get("TEST_CI"):
            image.show()
            old_image.show()
            diff.show()
        assert not diff.getbbox(), f"Images are different! {rtc3test_file}"

    pyboy.stop(save=False)
