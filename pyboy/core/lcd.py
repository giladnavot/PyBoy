#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

import logging
from array import array
from copy import deepcopy
from ctypes import c_void_p
from random import getrandbits

from pyboy.utils import color_code

logger = logging.getLogger(__name__)

VIDEO_RAM = 8 * 1024 # 8KB
OBJECT_ATTRIBUTE_MEMORY = 0xA0
INTR_VBLANK, INTR_LCDC, INTR_TIMER, INTR_SERIAL, INTR_HIGHTOLOW = [1 << x for x in range(5)]
ROWS, COLS = 144, 160
TILES = 384

FRAME_CYCLES = 70224

try:
    from cython import compiled
    cythonmode = compiled
except ImportError:
    cythonmode = False


class LCD:
    def __init__(self, cgb, cartridge_cgb, disable_renderer, color_palette, randomize=False):
        self.VRAM0 = array("B", [0] * VIDEO_RAM)
        self.OAM = array("B", [0] * OBJECT_ATTRIBUTE_MEMORY)

        if randomize:
            for i in range(VIDEO_RAM):
                self.VRAM0[i] = getrandbits(8)
            for i in range(OBJECT_ATTRIBUTE_MEMORY):
                self.OAM[i] = getrandbits(8)

        self._LCDC = LCDCRegister(0)
        self._STAT = STATRegister() # Bit 7 is always set.
        self.next_stat_mode = 2
        self.SCY = 0x00
        self.SCX = 0x00
        self.LY = 0x00
        self.LYC = 0x00
        # self.DMA = 0x00
        self.BGP = PaletteRegister(0xFC)
        self.OBP0 = PaletteRegister(0xFF)
        self.OBP1 = PaletteRegister(0xFF)
        self.WY = 0x00
        self.WX = 0x00
        self.clock = 0
        self.clock_target = 0
        self.frame_done = False
        self.double_speed = False
        self.cgb = cgb

        if self.cgb:
            if cartridge_cgb:
                logger.info("Starting CGB renderer")
                self.renderer = CGBRenderer(color_palette, color_palette, color_palette, CGB_NUM_PALETTES)
                # self.renderer = Renderer(False, color_palette, color_palette, color_palette)
            else:
                logger.info("Starting CGB renderer in DMG-mode")
                # Running DMG ROM on CGB hardware
                # use the default palettes
                bg_pal = (0xFFFFFF, 0x7BFF31, 0x0063C5, 0x000000)
                obj0_pal = (0xFFFFFF, 0xFF8484, 0xFF8484, 0x000000)
                obj1_pal = (0xFFFFFF, 0xFF8484, 0xFF8484, 0x000000)
                # self.renderer = CGBRenderer(bg_pal, obj0_pal, obj1_pal)
                self.renderer = Renderer(False, bg_pal, obj0_pal, obj1_pal, 1)
        else:
            logger.info("Starting DMG renderer")
            self.renderer = Renderer(False, color_palette, color_palette, color_palette, 1)

    def get_lcdc(self):
        return self._LCDC.value

    def set_lcdc(self, value):
        self._LCDC.set(value)

        if not self._LCDC.lcd_enable:
            # https://www.reddit.com/r/Gameboy/comments/a1c8h0/what_happens_when_a_gameboy_screen_is_disabled/
            # 1. LY (current rendering line) resets to zero. A few games rely on this behavior, namely Mr. Do! When LY
            # is reset to zero, no LYC check is done, so no STAT interrupt happens either.
            # 2. The LCD clock is reset to zero as far as I can tell.
            # 3. I believe the LCD enters Mode 0.
            self.clock = 0
            self.clock_target = FRAME_CYCLES # Doesn't render anything for the first frame
            self._STAT.set_mode(0)
            self.next_stat_mode = 2
            self.LY = 0

    def get_stat(self):
        return self._STAT.value

    def set_stat(self, value):
        self._STAT.set(value)

    def cycles_to_interrupt(self):
        return self.clock_target - self.clock

    def cycles_to_mode0(self):
        multiplier = 2 if self.double_speed else 1
        mode2 = 80 * multiplier
        mode3 = 170 * multiplier
        mode1 = 456 * multiplier

        mode = self._STAT._mode
        # Remaining cycles for this already active mode
        remainder = self.clock_target - self.clock

        if mode == 2:
            return remainder + mode3
        elif mode == 3:
            return remainder
        elif mode == 0:
            return 0
        elif mode == 1:
            remaining_ly = 153 - self.LY
            return remainder + mode1*remaining_ly + mode2 + mode3
        else:
            logger.error(f"Unsupported STAT mode: {mode}")
            return 0

    def processing_frame(self):
        b = (not self.frame_done)
        if not b:
            self.frame_done = False # Clear vblank flag for next iteration
        return b

    def tick(self, cycles):
        interrupt_flag = 0
        self.clock += cycles

        if self._LCDC.lcd_enable:
            if self.clock >= self.clock_target:
                # Change to next mode
                interrupt_flag |= self._STAT.set_mode(self.next_stat_mode)

                # Pan Docs:
                # The following are typical when the display is enabled:
                #   Mode 2  2_____2_____2_____2_____2_____2___________________2____
                #   Mode 3  _33____33____33____33____33____33__________________3___
                #   Mode 0  ___000___000___000___000___000___000________________000
                #   Mode 1  ____________________________________11111111111111_____
                multiplier = 2 if self.double_speed else 1

                # LCD state machine
                if self._STAT._mode == 2: # Searching OAM
                    if self.LY == 153:
                        self.LY = 0
                        self.clock %= FRAME_CYCLES
                        self.clock_target %= FRAME_CYCLES
                    else:
                        self.LY += 1

                    self.clock_target += 80 * multiplier
                    self.next_stat_mode = 3
                    interrupt_flag |= self._STAT.update_LYC(self.LYC, self.LY)
                elif self._STAT._mode == 3:
                    self.clock_target += 170 * multiplier
                    self.next_stat_mode = 0
                elif self._STAT._mode == 0: # HBLANK
                    self.clock_target += 206 * multiplier
                    self.renderer.update_cache(self)
                    self.renderer.scanline(self, self.LY)
                    self.renderer.scanline_sprites(self, self.LY, self.renderer._screenbuffer, False)
                    if self.LY < 143:
                        self.next_stat_mode = 2
                    else:
                        self.next_stat_mode = 1
                elif self._STAT._mode == 1: # VBLANK
                    self.clock_target += 456 * multiplier
                    self.next_stat_mode = 1

                    self.LY += 1
                    interrupt_flag |= self._STAT.update_LYC(self.LYC, self.LY)

                    if self.LY == 144:
                        interrupt_flag |= INTR_VBLANK
                        self.frame_done = True

                    if self.LY == 153:
                        # Reset to new frame and start from mode 2
                        self.next_stat_mode = 2
        else:
            # See also `self.set_lcdc`
            if self.clock >= FRAME_CYCLES:
                self.frame_done = True
                self.clock %= FRAME_CYCLES

                # Renderer
                self.renderer.blank_screen()

        return interrupt_flag

    def save_state(self, f):
        for n in range(VIDEO_RAM):
            f.write(self.VRAM0[n])

        for n in range(OBJECT_ATTRIBUTE_MEMORY):
            f.write(self.OAM[n])

        f.write(self._LCDC.value)
        f.write(self.BGP.value)
        f.write(self.OBP0.value)
        f.write(self.OBP1.value)

        f.write(self._STAT.value)
        f.write(self.LY)
        f.write(self.LYC)

        f.write(self.SCY)
        f.write(self.SCX)
        f.write(self.WY)
        f.write(self.WX)

        # CGB
        f.write(self.cgb)
        f.write(self.double_speed)
        if self.cgb:
            for n in range(VIDEO_RAM):
                f.write(self.VRAM1[n])
            f.write(self.vbk.active_bank)
            self.bcps.save_state(f)
            self.bcpd.save_state(f)
            self.ocps.save_state(f)
            self.ocpd.save_state(f)

    def load_state(self, f, state_version):
        for n in range(VIDEO_RAM):
            self.VRAM0[n] = f.read()

        for n in range(OBJECT_ATTRIBUTE_MEMORY):
            self.OAM[n] = f.read()

        self.set_lcdc(f.read())
        self.BGP.set(f.read())
        self.OBP0.set(f.read())
        self.OBP1.set(f.read())

        if state_version >= 5:
            self.set_stat(f.read())
            self.LY = f.read()
            self.LYC = f.read()

        self.SCY = f.read()
        self.SCX = f.read()
        self.WY = f.read()
        self.WX = f.read()

        # CGB
        _cgb = f.read()
        if self.cgb != _cgb:
            logger.critical(f"Loading state which is not CGB, but PyBoy is loaded in CGB mode!")
            return
        self.cgb = _cgb
        self.double_speed = f.read()
        if state_version >= 8 and self.cgb:
            for n in range(VIDEO_RAM):
                self.VRAM1[n] = f.read()
            self.vbk.active_bank = f.read()
            self.bcps.load_state(f, state_version)
            self.bcpd.load_state(f, state_version)
            self.ocps.load_state(f, state_version)
            self.ocpd.load_state(f, state_version)

    def getwindowpos(self):
        return (self.WX - 7, self.WY)

    def getviewport(self):
        return (self.SCX, self.SCY)


class PaletteRegister:
    def __init__(self, value):
        self.value = 0
        self.lookup = [0] * 4
        self.set(value)

    def set(self, value):
        # Pokemon Blue continuously sets this without changing the value
        if self.value == value:
            return False

        self.value = value
        for x in range(4):
            self.lookup[x] = (value >> x * 2) & 0b11
        return True

    def getcolor(self, i):
        return self.lookup[i]


class STATRegister:
    def __init__(self):
        self.value = 0b1000_0000
        self._mode = 0

    def set(self, value):
        value &= 0b0111_1000 # Bit 7 is always set, and bit 0-2 are read-only
        self.value &= 0b1000_0111 # Preserve read-only bits and clear the rest
        self.value |= value # Combine the two

    def update_LYC(self, LYC, LY):
        if LYC == LY:
            self.value |= 0b100 # Sets the LYC flag
            if self.value & 0b0100_0000: # LYC interrupt enabled flag
                return INTR_LCDC
        else:
            # Clear LYC flag
            self.value &= 0b1111_1011
        return 0

    def set_mode(self, mode):
        if self._mode == mode:
            # Mode already set
            return 0

        self._mode = mode
        self.value &= 0b11111100 # Clearing 2 LSB
        self.value |= mode # Apply mode to LSB

        # Check if interrupt is enabled for this mode
        # Mode "3" is not interruptable
        if mode != 3 and self.value & (1 << (mode + 3)):
            return INTR_LCDC
        return 0


class LCDCRegister:
    def __init__(self, value):
        self.set(value)

    def set(self, value):
        self.value = value

        # No need to convert to bool. Any non-zero value is true.
        # yapf: disable
        self.lcd_enable           = value & (1 << 7)
        self.windowmap_select     = value & (1 << 6)
        self.window_enable        = value & (1 << 5)
        self.tiledata_select      = value & (1 << 4)
        self.backgroundmap_select = value & (1 << 3)
        self.sprite_height        = value & (1 << 2)
        self.sprite_enable        = value & (1 << 1)
        self.background_enable    = value & (1 << 0)
        self.cgb_master_priority  = self.background_enable # Different meaning on CGB
        # yapf: enable


COL0_FLAG = 0b01
BG_PRIORITY_FLAG = 0b10


class Renderer:
    def __init__(self, cgb, color_palette, obj0_palette, obj1_palette, num_palettes):
        self.num_palettes = num_palettes
        self.cgb = cgb
        self.color_palette = [(c << 8) & ~COL0_FLAG for c in color_palette]
        self.obj0_palette = [(c << 8) & ~COL0_FLAG for c in obj0_palette]
        self.obj1_palette = [(c << 8) & ~COL0_FLAG for c in obj1_palette]
        self.color_format = "RGBA"

        self.buffer_dims = (ROWS, COLS)

        self.clearcache = False
        self.tiles_changed0 = set([])

        # Init buffers as white
        self._screenbuffer_raw = array("B", [0xFF] * (ROWS*COLS*4))
        self._tilecache0_raw = array("B", [0xFF] * (TILES*8*8*4*num_palettes))
        self._spritecache0_raw = array("B", [0xFF] * (TILES*8*8*4*num_palettes))
        self._spritecache1_raw = array("B", [0xFF] * (TILES*8*8*4*num_palettes))

        if cythonmode:
            self._screenbuffer = memoryview(self._screenbuffer_raw).cast("I", shape=(ROWS, COLS))
            self._tilecache0 = memoryview(self._tilecache0_raw).cast("I", shape=(num_palettes, TILES * 8, 8))
            self._spritecache0 = memoryview(self._spritecache0_raw).cast("I", shape=(num_palettes, TILES * 8, 8))
            self._spritecache1 = memoryview(self._spritecache1_raw).cast("I", shape=(num_palettes, TILES * 8, 8))
        else:
            stride = TILES * 8 * 8

            v = memoryview(self._screenbuffer_raw).cast("I")
            self._screenbuffer = [v[i:i + COLS] for i in range(0, COLS * ROWS, COLS)]
            self._screenbuffer_ptr = c_void_p(self._screenbuffer_raw.buffer_info()[0])

            v = memoryview(self._tilecache0_raw).cast("I")
            self._tilecache0 = [[v[i:i + 8] for i in range(stride * j, stride * (j+1), 8)] for j in range(num_palettes)]
            v = memoryview(self._spritecache0_raw).cast("I")
            self._spritecache0 = [[v[i:i + 8] for i in range(stride * j, stride * (j+1), 8)]
                                  for j in range(num_palettes)]

            v = memoryview(self._spritecache1_raw).cast("I")
            self._spritecache1 = [[v[i:i + 8] for i in range(stride * j, stride * (j+1), 8)]
                                  for j in range(num_palettes)]

        self._scanlineparameters = [[0, 0, 0, 0, 0] for _ in range(ROWS)]
        self.ly_window = 0

    def _cgb_get_background_map_attributes(self, lcd, i):
        tile_num = lcd.VRAM1[i]
        palette = tile_num & 0b111
        vbank = (tile_num >> 3) & 1
        horiflip = (tile_num >> 5) & 1
        vertflip = (tile_num >> 6) & 1
        bg_priority = (tile_num >> 7) & 1

        return palette, vbank, horiflip, vertflip, bg_priority

    def scanline(self, lcd, y):
        bx, by = lcd.getviewport()
        wx, wy = lcd.getwindowpos()
        self._scanlineparameters[y][0] = bx
        self._scanlineparameters[y][1] = by
        self._scanlineparameters[y][2] = wx
        self._scanlineparameters[y][3] = wy
        self._scanlineparameters[y][4] = lcd._LCDC.tiledata_select

        # All VRAM addresses are offset by 0x8000
        # Following addresses are 0x9800 and 0x9C00
        background_offset = 0x1800 if lcd._LCDC.backgroundmap_select == 0 else 0x1C00
        wmap = 0x1800 if lcd._LCDC.windowmap_select == 0 else 0x1C00

        # Used for the half tile at the left side when scrolling
        offset = bx & 0b111

        # Weird behavior, where the window has it's own internal line counter. It's only incremented whenever the
        # window is drawing something on the screen.
        if lcd._LCDC.window_enable and wy <= y and wx < COLS:
            self.ly_window += 1

        for x in range(COLS):
            if lcd._LCDC.window_enable and wy <= y and wx <= x:
                tile_addr = wmap + (self.ly_window) // 8 * 32 % 0x400 + (x-wx) // 8 % 32
                wt = lcd.VRAM0[tile_addr]
                # If using signed tile indices, modify index
                if not lcd._LCDC.tiledata_select:
                    # (x ^ 0x80 - 128) to convert to signed, then
                    # add 256 for offset (reduces to + 128)
                    wt = (wt ^ 0x80) + 128

                bg_priority_apply = 0x00
                if self.cgb:
                    palette, vbank, horiflip, vertflip, bg_priority = self._cgb_get_background_map_attributes(
                        lcd, tile_addr
                    )
                    tilecache = (self._tilecache1[palette] if vbank else self._tilecache0[palette])
                    xx = (7 - ((x-wx) % 8)) if horiflip else ((x-wx) % 8)
                    yy = (8*wt + (7 - (self.ly_window) % 8)) if vertflip else (8*wt + (self.ly_window) % 8)

                    if bg_priority:
                        # We hide extra rendering information in the lower 8 bits (A) of the 32-bit RGBA format
                        bg_priority_apply = BG_PRIORITY_FLAG
                else:
                    tilecache = self._tilecache0[0] # Fake palette index
                    xx = (x-wx) % 8
                    yy = 8*wt + (self.ly_window) % 8

                self._screenbuffer[y][x] = tilecache[yy][xx] | bg_priority_apply
            # background_enable doesn't exist for CGB. It works as master priority instead
            elif (not self.cgb and lcd._LCDC.background_enable) or self.cgb:
                tile_addr = background_offset + (y+by) // 8 * 32 % 0x400 + (x+bx) // 8 % 32
                bt = lcd.VRAM0[tile_addr]
                # If using signed tile indices, modify index
                if not lcd._LCDC.tiledata_select:
                    # (x ^ 0x80 - 128) to convert to signed, then
                    # add 256 for offset (reduces to + 128)
                    bt = (bt ^ 0x80) + 128

                bg_priority_apply = 0x00
                if self.cgb:
                    palette, vbank, horiflip, vertflip, bg_priority = self._cgb_get_background_map_attributes(
                        lcd, tile_addr
                    )
                    tilecache = (self._tilecache1[palette] if vbank else self._tilecache0[palette])
                    xx = (7 - ((x+offset) % 8)) if horiflip else ((x+offset) % 8)
                    yy = (8*bt + (7 - (y+by) % 8)) if vertflip else (8*bt + (y+by) % 8)

                    if bg_priority:
                        # We hide extra rendering information in the lower 8 bits (A) of the 32-bit RGBA format
                        bg_priority_apply = BG_PRIORITY_FLAG
                else:
                    tilecache = self._tilecache0[0] # Fake palette index
                    xx = (x+offset) % 8
                    yy = 8*bt + (y+by) % 8

                self._screenbuffer[y][x] = tilecache[yy][xx] | bg_priority_apply
            else:
                # If background is disabled, it becomes white
                self._screenbuffer[y][x] = self.color_palette[0]

        if y == 143:
            # Reset at the end of a frame. We set it to -1, so it will be 0 after the first increment
            self.ly_window = -1

    def key_priority(self, x):
        # NOTE: Cython is being insufferable, and demands a non-lambda function
        return (self.sprites_to_render_x[x], self.sprites_to_render_n[x])

    def scanline_sprites(self, lcd, ly, buffer, ignore_priority):

        if not lcd._LCDC.sprite_enable:
            return

        spriteheight = 16 if lcd._LCDC.sprite_height else 8

        sprite_count = 0
        self.sprites_to_render_n = [0] * 10
        self.sprites_to_render_x = [0] * 10

        # Find the first 10 sprites in OAM that appears on this scanline.
        # The lowest X-coordinate has priority, when overlapping

        # Loop through OAM, find 10 first sprites for scanline. Order based on X-coordinate high-to-low. Render them.
        for n in range(0x00, 0xA0, 4):
            y = lcd.OAM[n] - 16 # Documentation states the y coordinate needs to be subtracted by 16
            x = lcd.OAM[n + 1] - 8 # Documentation states the x coordinate needs to be subtracted by 8

            if y <= ly < y + spriteheight:
                self.sprites_to_render_n[sprite_count] = n
                self.sprites_to_render_x[sprite_count] = x # Used for sorting for priority
                sprite_count += 1

            if sprite_count == 10:
                break

        # Pan docs:
        # When these 10 sprites overlap, the highest priority one will appear above all others, etc. (Thus, no
        # Z-fighting.) In CGB mode, the first sprite in OAM ($FE00-$FE03) has the highest priority, and so on. In
        # Non-CGB mode, the smaller the X coordinate, the higher the priority. The tie breaker (same X coordinates) is
        # the same priority as in CGB mode.
        sprites_priority = sorted(range(sprite_count), key=self.key_priority)

        for _n in sprites_priority[::-1]:
            n = self.sprites_to_render_n[_n]
            y = lcd.OAM[n] - 16 # Documentation states the y coordinate needs to be subtracted by 16
            x = lcd.OAM[n + 1] - 8 # Documentation states the x coordinate needs to be subtracted by 8
            tileindex = lcd.OAM[n + 2]
            if spriteheight == 16:
                tileindex &= 0b11111110
            attributes = lcd.OAM[n + 3]
            xflip = attributes & 0b00100000
            yflip = attributes & 0b01000000
            spritepriority = (attributes & 0b10000000) and not ignore_priority
            if self.cgb:
                palette = attributes & 0b111
                spritecache = self._spritecache1[palette] if attributes & 0b1000 else self._spritecache0[palette]
            else:
                # Fake palette index
                spritecache = self._spritecache1[0] if attributes & 0b10000 else self._spritecache0[0]

            dy = ly - y
            yy = spriteheight - dy - 1 if yflip else dy

            for dx in range(8):
                xx = 7 - dx if xflip else dx
                pixel = spritecache[8*tileindex + yy][xx]
                if 0 <= x < COLS:
                    if self.cgb:
                        bgmappriority = buffer[ly][x] & BG_PRIORITY_FLAG

                        if lcd._LCDC.cgb_master_priority: # If 0, sprites are always on top, if 1 follow priorities
                            if bgmappriority: # If 0, use spritepriority, if 1 take priority
                                if buffer[ly][x] & COL0_FLAG:
                                    buffer[ly][x] = pixel
                            elif spritepriority: # If 1, sprite is behind bg/window. Color 0 of window/bg is transparent
                                if buffer[ly][x] & COL0_FLAG:
                                    buffer[ly][x] = pixel
                            else:
                                if not pixel & COL0_FLAG: # If pixel is not transparent
                                    buffer[ly][x] = pixel
                        else:
                            if not pixel & COL0_FLAG: # If pixel is not transparent
                                buffer[ly][x] = pixel
                    else:
                        if spritepriority: # If 1, sprite is behind bg/window. Color 0 of window/bg is transparent
                            if buffer[ly][x] & COL0_FLAG: # if BG pixel is transparent
                                if not pixel & COL0_FLAG: # If pixel is not transparent
                                    buffer[ly][x] = pixel
                        else:
                            if not pixel & COL0_FLAG: # If pixel is not transparent
                                buffer[ly][x] = pixel
                x += 1
            x -= 8

    def render_sprites(self, lcd, buffer, ignore_priority):
        # NOTE: LEGACY FUNCTION FOR DEBUG WINDOW! Use scanline_sprites instead

        # Render sprites
        # - Doesn't restrict 10 sprites per scan line
        # - Prioritizes sprite in inverted order
        spriteheight = 16 if lcd._LCDC.sprite_height else 8

        sprites_on_ly = [0] * 144

        for n in range(0x00, 0xA0, 4):
            y = lcd.OAM[n] - 16 # Documentation states the y coordinate needs to be subtracted by 16
            x = lcd.OAM[n + 1] - 8 # Documentation states the x coordinate needs to be subtracted by 8
            tileindex = lcd.OAM[n + 2]
            attributes = lcd.OAM[n + 3]
            xflip = attributes & 0b00100000
            yflip = attributes & 0b01000000
            spritepriority = (attributes & 0b10000000) and not ignore_priority
            spritecache = (
                self._spritecache1[0] if attributes & 0b10000 else self._spritecache0[0]
            ) # Fake palette index

            for dy in range(spriteheight):
                yy = spriteheight - dy - 1 if yflip else dy

                # Take care of sprite priorty. No more than 10 sprites per scanline
                if 0 <= y < 144:
                    if sprites_on_ly[y] >= 10:
                        continue
                    else:
                        sprites_on_ly[y] += 1

                if 0 <= y < ROWS:
                    for dx in range(8):
                        xx = 7 - dx if xflip else dx
                        pixel = spritecache[8*tileindex + yy][xx]
                        if 0 <= x < COLS:
                            if spritepriority: # If 1, sprite is behind bg/window. Color 0 of window/bg is transparent
                                if buffer[y][x] & COL0_FLAG: # if BG pixel is transparent
                                    if not pixel & COL0_FLAG: # If pixel is not transparent
                                        buffer[y][x] = pixel
                            else:
                                if not pixel & COL0_FLAG: # If pixel is not transparent
                                    buffer[y][x] = pixel
                        x += 1
                    x -= 8
                y += 1

    def update_cache(self, lcd):
        if self.clearcache:
            self.tiles_changed0.clear()
            for x in range(0x8000, 0x9800, 16):
                self.tiles_changed0.add(x)
            self.clearcache = False

        for t in self.tiles_changed0:
            for k in range(0, 16, 2): # 2 bytes for each line
                byte1 = lcd.VRAM0[t + k - 0x8000]
                byte2 = lcd.VRAM0[t + k + 1 - 0x8000]
                y = (t+k-0x8000) // 2

                for x in range(8):
                    colorcode = color_code(byte1, byte2, 7 - x)

                    self._tilecache0[0][y][x] = self.color_palette[lcd.BGP.getcolor(colorcode)]
                    self._spritecache0[0][y][x] = self.obj0_palette[lcd.OBP0.getcolor(colorcode)]
                    self._spritecache1[0][y][x] = self.obj1_palette[lcd.OBP1.getcolor(colorcode)]

                    if colorcode == 0:
                        self._spritecache0[0][y][x] |= COL0_FLAG
                        self._spritecache1[0][y][x] |= COL0_FLAG
                        self._tilecache0[0][y][x] |= COL0_FLAG
                    else:
                        self._spritecache0[0][y][x] &= ~COL0_FLAG
                        self._spritecache1[0][y][x] &= ~COL0_FLAG
                        self._tilecache0[0][y][x] &= ~COL0_FLAG

        self.tiles_changed0.clear()

    def blank_screen(self):
        # If the screen is off, fill it with a color.
        color = self.color_palette[0]
        for y in range(ROWS):
            for x in range(COLS):
                self._screenbuffer[y][x] = color

    def save_state(self, f):
        for y in range(ROWS):
            f.write(self._scanlineparameters[y][0])
            f.write(self._scanlineparameters[y][1])
            # We store (WX - 7). We add 7 and mask 8 bits to make it easier to serialize
            f.write((self._scanlineparameters[y][2] + 7) & 0xFF)
            f.write(self._scanlineparameters[y][3])
            f.write(self._scanlineparameters[y][4])

        for y in range(ROWS):
            for x in range(COLS):
                z = self._screenbuffer[y][x]
                f.write_32bit(z)

    def load_state(self, f, state_version):
        if state_version >= 2:
            for y in range(ROWS):
                self._scanlineparameters[y][0] = f.read()
                self._scanlineparameters[y][1] = f.read()
                # Restore (WX - 7) as described above
                self._scanlineparameters[y][2] = (f.read() - 7) & 0xFF
                self._scanlineparameters[y][3] = f.read()
                if state_version > 3:
                    self._scanlineparameters[y][4] = f.read()

        if state_version >= 6:
            for y in range(ROWS):
                for x in range(COLS):
                    self._screenbuffer[y][x] = f.read_32bit()

        self.clearcache = True


####################################
#
#  ██████   ██████   ██████
# ██       ██        ██   ██
# ██       ██   ███  ██████
# ██       ██    ██  ██   ██
#  ██████   ██████   ██████
#
CGB_NUM_PALETTES = 8


class CGBLCD(LCD):
    def __init__(self, cgb, cartridge_cgb, disable_renderer, color_palette, randomize=False):
        LCD.__init__(self, cgb, cartridge_cgb, disable_renderer, color_palette, randomize=False)
        self.VRAM1 = array("B", [0] * VIDEO_RAM)

        self.vbk = VBKregister()
        self.bcps = PaletteIndexRegister()
        self.bcpd = PaletteColorRegister(self.bcps)
        self.ocps = PaletteIndexRegister()
        self.ocpd = PaletteColorRegister(self.ocps)


class CGBRenderer(Renderer):
    def __init__(self, color_palette, obj0_palette, obj1_palette, num_palettes):
        Renderer.__init__(self, True, color_palette, obj0_palette, obj1_palette, num_palettes)
        self.tiles_changed1 = set([])

        self._tilecache1_raw = array("B", [0xFF] * (TILES*8*8*4*num_palettes))

        if cythonmode:
            self._tilecache1 = memoryview(self._tilecache1_raw).cast("I", shape=(num_palettes, TILES * 8, 8))
        else:
            stride = TILES * 8 * 8
            v = memoryview(self._tilecache1_raw).cast("I")
            self._tilecache1 = [[v[i:i + 8] for i in range(stride * j, stride * (j+1), 8)] for j in range(num_palettes)]

    def key_priority(self, x):
        # Define sprite sorting for CGB
        return (self.sprites_to_render_n[x], self.sprites_to_render_x[x])

    def update_cache(self, lcd):
        if self.clearcache:
            self.tiles_changed0.clear()
            self.tiles_changed1.clear()
            for x in range(0x8000, 0x9800, 16):
                self.tiles_changed0.add(x)
                self.tiles_changed1.add(x)
            self.clearcache = False
        self.update_tiles(lcd, self.tiles_changed0, 0)
        self.update_tiles(lcd, self.tiles_changed1, 1)
        self.tiles_changed0.clear()
        self.tiles_changed1.clear()

    def update_tiles(self, lcd, tiles_changed, bank):
        if bank:
            vram_bank = lcd.VRAM1
            tilecache_bank = self._tilecache1
            spritecache_bank = self._spritecache1
        else:
            vram_bank = lcd.VRAM0
            tilecache_bank = self._tilecache0
            spritecache_bank = self._spritecache0

        for t in tiles_changed:
            for k in range(0, 16, 2): # 2 bytes for each line
                byte1 = vram_bank[t + k - 0x8000]
                byte2 = vram_bank[t + k + 1 - 0x8000]

                y = (t+k-0x8000) // 2

                # update for the 8 palettes
                for p in range(self.num_palettes):
                    for x in range(8):
                        #index into the palette for the current pixel
                        colorcode = color_code(byte1, byte2, 7 - x)

                        tilecache_bank[p][y][x] = (lcd.bcpd.getcolor(p, colorcode) << 8)
                        spritecache_bank[p][y][x] = (lcd.ocpd.getcolor(p, colorcode) << 8)
                        if colorcode == 0: # first color is used for transparency when applicable
                            spritecache_bank[p][y][x] |= COL0_FLAG
                            tilecache_bank[p][y][x] |= COL0_FLAG
                        else:
                            spritecache_bank[p][y][x] &= ~COL0_FLAG
                            tilecache_bank[p][y][x] &= ~COL0_FLAG


class VBKregister:
    def __init__(self, value=0):
        self.active_bank = value

    def set(self, value):
        # when writing to VBK, bit 0 indicates which bank to switch to
        bank = value & 1
        self.active_bank = bank

    def get(self):
        # reading from this register returns current VRAM bank in bit 0, other bits = 1
        return self.active_bank | 0xFE


class PaletteIndexRegister:
    def __init__(self, val=0):
        self.value = val
        self.auto_inc = 0
        self.index = 0
        self.hl = 0

    def set(self, val):
        if self.value == val:
            return
        self.value = val
        self.hl = val & 0b1
        self.index = (val >> 1) & 0b11111
        self.auto_inc = (val >> 7) & 0b1

    def get(self):
        return self.value

    def getindex(self):
        return self.index

    def shouldincrement(self):
        if self.auto_inc:
            # ensure autoinc also set for new val
            new_val = 0x80 | (self.value + 1)
            self.set(new_val)

    def save_state(self, f):
        f.write(self.value)
        f.write(self.auto_inc)
        f.write(self.index)
        f.write(self.hl)

    def load_state(self, f, state_version):
        self.value = f.read()
        self.auto_inc = f.read()
        self.index = f.read()
        self.hl = f.read()


class PaletteColorRegister:
    def __init__(self, i_reg):
        #8 palettes of 4 colors each 2 bytes
        self.palette_mem = array("I", [0xFFFF] * CGB_NUM_PALETTES * 4)

        # Init with some colors -- TODO: What are real defaults?
        for n in range(0, len(self.palette_mem), 4):
            c = [0x1CE7, 0x1E19, 0x7E31, 0x217B]
            for m in range(4):
                self.palette_mem[n + m] = c[m]

        self.index_reg = i_reg

    def set(self, val):
        i_val = self.palette_mem[self.index_reg.getindex()]
        if self.index_reg.hl:
            self.palette_mem[self.index_reg.getindex()] = (i_val & 0x00FF) | (val << 8)
        else:
            self.palette_mem[self.index_reg.getindex()] = (i_val & 0xFF00) | val

        #check for autoincrement after write
        self.index_reg.shouldincrement()

    def get(self):
        return self.palette_mem[self.index_reg.getindex()]

    def getcolor(self, paletteindex, colorindex):
        #each palette = 8 bytes or 4 colors of 2 bytes
        if paletteindex > 7 or colorindex > 3:
            logger.error(f"Palette Mem Index Error, tried: Palette {paletteindex} color {colorindex}")

        i = paletteindex*4 + colorindex
        cgb_color = self.palette_mem[i] & 0x7FFF

        red = (cgb_color & 0x1F) << 3
        green = ((cgb_color >> 5) & 0x1F) << 3
        blue = ((cgb_color >> 10) & 0x1F) << 3

        rgb_color = (red << 16) | (green << 8) | blue

        return rgb_color

    def save_state(self, f):
        for n in range(CGB_NUM_PALETTES * 4):
            f.write_16bit(self.palette_mem[n])

    def load_state(self, f, state_version):
        for n in range(CGB_NUM_PALETTES * 4):
            self.palette_mem[n] = f.read_16bit()
