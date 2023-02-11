import pytest
import rnsh.exception as exception

def test_permit():
    with pytest.raises(SystemExit):
        with exception.permit(SystemExit):
            raise Exception("Should not bubble")
        with exception.permit(SystemExit):
            raise SystemExit()