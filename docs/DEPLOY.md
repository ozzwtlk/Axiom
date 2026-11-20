# Server + computing machines (Raspberry Pi setup)

```
 phone / laptop browser ──►  Raspberry Pi (docker)           ◄── PC:     ./axiom worker   (Qwen + Claude/Codex)
                             stores books, database, images  ◄── laptop: ./axiom worker   (Claude/Codex only)
                             serves the reader, computes nothing
```

* The **server** (Pi) keeps all data and serves the reader at `http://<pi-address>:8080`. It never runs a model,
  parses a PDF or renders a page. Anything that needs computing is forwarded to a connected machine, and when
  none is connected the reader still works; AI buttons say that no machine is connected.
* A **computing machine** (`./axiom worker`) connects *out* to the server, so it works through a laptop firewall.
  It processes books from the server's queue and answers AI requests (questions, hints, solutions, exams,
  visualizations, page images). It reports which models it has: the local Qwen model if it is running there,
  Claude Code and Codex if their CLIs are signed in. Nothing is stored on it except a cache.
* Pick the machine in the reader (top-right dropdown) or under **AI models → Computing machines**. **Auto**
  prefers a machine with a local model. Processing jobs only run on a machine that has the model they need,
  so a Qwen run waits for your PC and is never attempted on the laptop.
* Data never needs syncing: everything is written straight to the server. Uploads made while every PC is off wait
  in the server's inbox and are imported when one connects; queued chapters continue the same way. Page images
  and scan crops are stored on the server after first use, so they also work when the PCs are off.

## 1. Server (Raspberry Pi, Docker)

```bash
git clone <this repo> axiom && cd axiom      # or copy the repo to the Pi
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(24))"   # paste as AXIOM_TOKEN in .env
docker compose up -d --build                 # first build takes a few minutes on a Pi 5
```

Open `http://<pi-address>:8080` (change `AXIOM_PORT` in `.env`). Data lives in `./server-data` (`AXIOM_DATA`); an
SSD is better than the SD card for the database. Update with `git pull && docker compose up -d --build`.

## 2. Move your existing books to the server (once)

On the PC, with the server container stopped (`docker compose stop` on the Pi):

```bash
./axiom stop
./axiom migrate-to-server pi@<pi-address>:/home/pi/axiom/server-data
```

Then on the Pi: `sudo chown -R 1000:1000 server-data && docker compose up -d`.

## 3. Computing machines (PC with Qwen, laptop with subscriptions)

```bash
./axiom setup-worker http://<pi-address>:8080 main-pc     # asks for the AXIOM_TOKEN, stores .axiom-worker.env
./axiom                                                   # = ./axiom worker once set up
```

On the PC this also starts the local model; on a laptop without one it connects with Claude Code / Codex only (sign in
once with `claude` / `codex login`). `./axiom stop` disconnects; running jobs go back to the queue and resume
(per-section checkpoints and the model cache mean nothing is redone).

## Security

* `/internal/*` (database, files, worker socket) requires the `AXIOM_TOKEN`; keep it secret.
* The reader itself has no login. It is meant for your home network. Do not forward the port to the internet;
  use a VPN such as WireGuard or Tailscale to reach it from outside.
* The subscription CLIs still run with all tools disabled on the computing machines (see README).
