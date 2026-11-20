# The official LiteLLM proxy image with corveil-litellm installed by pip, as
# the README tells users to do. Nothing is mounted next to config.yaml, so the
# proxy can only find the callback as an installed package.
#
#   LITELLM_IMAGE  the proxy image to build on (default: the stable tag)
#   INSTALL        what to pip install: the wheel run.sh built from this
#                  checkout (the default), or a release such as
#                  corveil-litellm==0.5.0 to check what PyPI serves

ARG LITELLM_IMAGE=ghcr.io/berriai/litellm:main-stable
FROM ${LITELLM_IMAGE}
ARG INSTALL="/tmp/dist/*.whl"
COPY .work/dist/ /tmp/dist/
RUN python3 -m ensurepip \
 && python3 -m pip install --no-cache-dir ${INSTALL} \
 && rm -rf /tmp/dist
