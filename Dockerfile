# A bot in a container: one image, one instance, its config mounted and its database in a volume.
#
# This is how most people will run a game bot, and it removes "does it work on my distro" as a
# question — the systemd unit in docs/deployment.md is no longer the only supported way.
#
# Two things about it are deliberate and easy to get wrong the other way:
#
# * **The image is the version.** `b3 update` detects a container and says to pull a new image
#   instead of running pip inside one, because a pip install in a container is thrown away with it.
# * **Nothing about a server lives in the image.** The config is mounted and the database is a
#   volume, so the same image runs every one of an operator's servers — which is the same deployment
#   model as the shared code install on a host.

FROM python:3.13-slim AS build

# git is needed at *build* time only if a plugin is installed into the image; the runtime stage
# below carries it too, because `b3 plugin install` and `b3 update --check` shell out to it.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# Built and installed into a virtualenv that is copied whole into the runtime image, so the compiler
# toolchain and the pip cache never reach it.
RUN python -m venv /opt/b3 \
    && /opt/b3/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/b3/bin/pip install --no-cache-dir .


FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/* \
    # A game bot has no business being root: it reads a log, talks to a game server over UDP or TCP,
    # and writes one database. Created with a fixed uid so a mounted volume's ownership is
    # predictable on the host.
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin b3

COPY --from=build /opt/b3 /opt/b3
ENV PATH="/opt/b3/bin:$PATH" \
    # Unbuffered, or `docker logs` shows nothing until the bot has something to flush.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The config is mounted here read-only; the database and any installed plugins live in the volume.
WORKDIR /data
VOLUME ["/data"]
USER b3

# No healthcheck: a bot with nothing to do is indistinguishable from a bot that is stuck, from
# outside. `b3 doctor` is the honest answer to "is this install working?", and it is a command an
# operator runs rather than something to poll.
ENTRYPOINT ["b3", "-c", "/data/b3.yaml"]
CMD ["run"]
