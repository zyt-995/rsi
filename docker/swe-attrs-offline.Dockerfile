FROM swe-chain:attrs-21.3.0

ARG ATTRS_VERSION
COPY --from=wheelhouse / /tmp/rsi-wheelhouse/
RUN pip install --no-index --find-links=/tmp/rsi-wheelhouse \
    hatchling hatch-vcs hatch-fancy-pypi-readme setuptools-scm wheel editables

WORKDIR /app/code
COPY . /app/code
RUN SETUPTOOLS_SCM_PRETEND_VERSION="${ATTRS_VERSION}" \
    uv pip install -e . --no-deps --no-build-isolation --offline --system -q
