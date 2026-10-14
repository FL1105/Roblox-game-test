# Running the recorder on a server

Statistical conclusions need days of real launches, which a laptop can't reliably provide. A small rented server can.

1. Rent the cheapest Ubuntu server (about $5/month) from any provider (Hetzner, DigitalOcean, Vultr...). Pick Ubuntu 22.04 or 24.04.
2. Log in over SSH as root (your provider shows you how) and run:

       curl -fsSL https://raw.githubusercontent.com/FL1105/Roblox-game-test/claude/loving-cori-dvrk02/deploy/install_recorder.sh | bash

   (Or read the script first; it is short and only installs Python and this project.)
3. That's it. It records in the background, restarts itself after crashes or reboots, and writes one file per day to `/var/lib/sniper/`.
4. Bring the data home with `scp root@YOUR_SERVER:/var/lib/sniper/*.jsonl .` (or send the files to me).

What it holds: nothing sensitive. It has no wallet, no keys and no passwords, and it only reads public launch feeds and public price quotes.
Untested on a real server (this project was built in a sandbox): if a step fails, send me the error text.
