# Reproduce the gateway acceptance demo

The demo runs a host and two authenticated non-member profiles through the React SDK, Django, the ASGI gateway, Janus and coturn. One guest publishes audio. The other is denied in the lobby. Chromium uses synthetic microphones and verifies real RTP counters. The same command runs in the frontend workflow.

## Prepare a clean machine

Use a disposable Ubuntu 24.04 x86-64 VM with Bash, Docker access and permission to clone the private `Leydotpy/synq.js` repository. The test account creates and drops its own PostgreSQL test database. The failure rehearsals pause their isolated Redis service and restart their own Janus process. Run these commands in the same shell.

```bash
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y ca-certificates curl git xz-utils openssl python3-venv janus coturn redis-server
demo_root=$(mktemp -d)
cd "$demo_root"

# Use the same Node, Bun, uv and Playwright versions as the browser rehearsal.
node_archive=node-v24.22.3-linux-x64.tar.xz
curl -fsSLO "https://nodejs.org/dist/v24.22.3/$node_archive"
curl -fsSLo node-checksums https://nodejs.org/dist/v24.22.3/SHASUMS256.txt
awk -v name="$node_archive" '$2 == name {print}' node-checksums | sha256sum --check
tar -xJf "$node_archive"
export PATH="$demo_root/node-v24.22.3-linux-x64/bin:$PATH"
python3 -m venv tools
tools/bin/pip install uv==0.13.4
npm install --prefix "$demo_root/node-tools" bun@1.3.2
export PATH="$demo_root/tools/bin:$demo_root/node-tools/node_modules/.bin:$PATH"
uv python install 3.14

git clone --single-branch --branch codex/gateway-foundation https://github.com/Leydotpy/jrtc-signalling.git gateway
git clone --single-branch --branch codex/gateway-control-plane https://github.com/Leydotpy/synq.git synq
git clone --single-branch --branch codex/gateway-transport https://github.com/Leydotpy/synq.js.git client
git -C gateway checkout --detach 2c4e6d623f265b6aa2ba28a3b4bcca417d1c0c4c
git -C synq checkout --detach af07fc5dcdc9d284a6fbd85f26f2e1aaaf417f8c
git -C client checkout --detach 7a1963bd271fc8d0d1e19cdacfa7c16613d8a614
```

The Synq lockfile installs gateway runtime `8ad22c05f13a87d04a16189a8ec08bcc0276cec6`. The gateway checkout above includes the final acceptance tests. Both revisions have the same runtime source, packaging and dependency lock.

## Start disposable backing services

Docker chooses two loopback ports. The database reports constraint failures without writing row contents or SQL statements to the rehearsal logs.

```bash
export UV_PYTHON=3.14
export GATEWAY_TEST_DB_PASSWORD="$(openssl rand -hex 24)"
pg_container=$(docker run --rm -d -p 127.0.0.1::5432 \
  -e POSTGRES_DB=synq_gateway_test -e POSTGRES_PASSWORD="$GATEWAY_TEST_DB_PASSWORD" \
  postgres:16 -c log_error_verbosity=terse -c log_min_error_statement=panic)
redis_container=$(docker run --rm -d -p 127.0.0.1::6379 redis:7.4)
trap 'docker stop "$redis_container" "$pg_container" >/dev/null' EXIT
export GATEWAY_TEST_DB_PORT="$(docker port "$pg_container" 5432/tcp | cut -d: -f2)"
redis_port=$(docker port "$redis_container" 6379/tcp | cut -d: -f2)
export GATEWAY_TEST_REDIS_URL="redis://127.0.0.1:$redis_port/15"
for attempt in {1..60}; do
  if docker exec "$pg_container" pg_isready -q -U postgres &&
     docker exec "$redis_container" redis-cli ping | grep -qx PONG; then break; fi
  sleep 1
done
docker exec "$pg_container" pg_isready -U postgres
docker exec "$redis_container" redis-cli ping | grep -qx PONG
```

## Build and run

```bash
(cd gateway && uv sync --locked --group dev --group media-test --extra runtime &&
  uv run --no-sync jrtc-signalling --check-config && uv build)
bash client/scripts/verify-gateway-browser.sh synq gateway
```

The browser command installs locked workspace packages, builds and typechecks the SDK and app, runs their test suites, and installs Playwright 1.57.0 with Chromium. It then starts disposable HTTPS/WSS servers, Janus, TURN and signed Broka command and event workers. The Django fixture creates real session cookies for the three profiles. A successful run prints the browser version, `mediaPeers: 4`, the list of completed checks, and Django's `OK`. Four peers are the publisher and subscriber connections for each of the two audio participants. The denied guest has zero media peers.

## Repeat the failure and rollback checks

Run the commands sequentially against the disposable services above. The multiworker runner needs local TLS loopback and the benchmark needs the installed `redis-server` executable. The canary uses Redis database 14, separately from the other integrations on database 15.

```bash
export DJANGO_SETTINGS_MODULE=conf.gateway_postgres_test_settings
(cd gateway && GATEWAY_TEST_JANUS=1 uv run --no-sync pytest -q tests/test_janus_live.py)
cd "$demo_root/synq"
uv run --frozen --with pytest --with aiortc python ../gateway/scripts/check_django_moderation.py --synq .
uv run --frozen --with pytest --with aiortc python ../gateway/scripts/check_django_lifecycle.py --synq .
uv run --frozen --with pytest --with aiortc python scripts/check_gateway_state.py --gateway ../gateway
uv run --frozen --with pytest --with aiortc python scripts/check_gateway_multiworker.py --gateway ../gateway
GATEWAY_TEST_REDIS_URL="redis://127.0.0.1:$redis_port/14" \
  uv run --frozen --with pytest --with aiortc python scripts/check_gateway_cutover.py --gateway ../gateway
for first in legacy gateway; do
  uv run --frozen --with pytest --with aiortc python scripts/benchmark_gateway_ice.py --gateway ../gateway --first "$first"
done
```

The canary reports `SG17_CANARY_OK` after the original Janus session has gone and the replacement legacy session sends audio. The benchmark prints one `SG16_RESULT` per order. Exiting the shell stops the two containers. The browser and media runners close their own temporary processes and files.

[Final acceptance report](implementation/tracking/reports/SG-18.md) · [Staged routing and failback](STAGED_CUTOVER.md)
