import logging
import rnsh.rnsh
logging.getLogger().setLevel(logging.DEBUG)


def test_check_magic():
    magic = rnsh.rnsh._PROTOCOL_VERSION_0
    # magic for version 0 is generated, make sure it comes out as expected
    assert magic == 0xdeadbeef00000000
    # verify the checker thinks it's right
    assert rnsh.rnsh._protocol_check_magic(magic)
    # scramble the magic
    magic = magic | 0x00ffff0000000000
    # make sure it fails now
    assert not rnsh.rnsh._protocol_check_magic(magic)
