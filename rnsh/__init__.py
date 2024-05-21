# MIT License
#
# Copyright (c) 2023 Aaron Heise
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
module_abs_filename = os.path.abspath(__file__)
module_dir = os.path.dirname(module_abs_filename)

def _get_version():
    def pkg_res_version():
        import pkg_resources
        return pkg_resources.get_distribution("rnsh").version

    def tomli_version():
        import tomli
        return tomli.load(open(os.path.join(os.path.dirname(module_dir), "pyproject.toml"), "rb"))["tool"]["poetry"]["version"]

    try:
        if (os.path.isfile(os.path.join(os.path.dirname(module_dir), "pyproject.toml"))):
            try:
                return tomli_version()
            except:
                return "0.0.0"
        else:
            try:
                return pkg_res_version()
            except:
                return "0.0.0"
                
    except:
        return "0.0.0"

__version__ = _get_version()