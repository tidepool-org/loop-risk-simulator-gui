"""The app's version number -- the single source for both the running app and the
release bundle it is packaged into.

Kept in its own module, deliberately, rather than in ``streamlit_app.py``:
``packaging/build_bundle.py`` must stay stdlib-only and importing the app would
pull streamlit, pandas and the simulator into the build. Nothing else works in
both places -- there is no ``pyproject.toml``/``setup.py``, so
``importlib.metadata`` has no distribution to read; ``BUNDLE_VERSION.json`` exists
only inside a built bundle; and ``git describe`` fails in the shipped tarball.

The number means the LAST RELEASED VERSION, and is bumped by hand as part of
making a release -- not on ticket merge. See README, "Versioning", for what each
component means (SOP-0005 §7.1). 1.0.0 is the MVP internal release.
"""

APP_VERSION = "1.0.0"
