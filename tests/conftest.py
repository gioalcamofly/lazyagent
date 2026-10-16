# Some tests build pyte screens or bind pyte.screens.Char before importing any
# lazyagent widget, so apply the patch up front the way the widgets do.
import lazyagent.pyte_patch  # noqa: F401
