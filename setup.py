"""Build step: put ``project.bashrc`` into the package of a wheel.

The file stays at ``aisland/project.bashrc`` in the checkout — the workspace root's ``project.bashrc`` links to it,
and the file finds the root as its own folder's parent — so it cannot move into the package. A wheel still needs it:
from a wheel ``aisland setup`` copies it into ``<workspace>/.aisland/`` (``aisland.setup.bashrc``). An
editable install reads the checkout's file and needs no copy. ``MANIFEST.in`` puts the file into the source package,
from which ``python -m build`` builds the wheel. Pinned by
``tests/test_packaging.py::test_the_wheel_carries_the_shell_init_beside_the_package`` and
``::test_a_release_build_puts_the_shell_init_into_the_wheel``.
"""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

SHELL_INIT = Path(__file__).parent / "project.bashrc"


class BuildPyWithShellInit(build_py):
    """``build_py`` that also copies ``project.bashrc`` into the built package."""

    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            return
        self.copy_file(str(SHELL_INIT), str(Path(self.build_lib) / "aisland" / "project.bashrc"))


setup(cmdclass={"build_py": BuildPyWithShellInit})
