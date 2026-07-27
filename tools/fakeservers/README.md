# Fake game servers

Small, honest implementations of the protocols the bot speaks, for exercising it without owning a
copy of every game.

They exist because **every serious bug this project has found appeared in a live run and not in the
unit suite** — the lost ban on a dead RCON, the off-loop event drop, the buffered game log, the second
empty database. A fake server is the closest thing to a live run that fits in CI.

They live here, in the repository, rather than in a scratch directory: the previous set was written in
a session scratchpad and did not survive it, so the next protocol had to start from nothing.

## What is here

| File | Speaks | Used by |
|---|---|---|
| `battleye.py` | BattlEye RCON (Arma 2/3) — login, commands, pushed server messages, ACKs, multi-part replies, a stateful ban list | `tests/test_battleye_*.py`, `e2e_battleye.py` |
| `frostbite.py` | Frostbite RCON (BF3/BF4/BC2/MoH) — binary TCP word lists, a real salt-and-hash login, opt-in events, ACKs, deliberate stream fragmentation | `tests/test_frostbite_net.py`, `e2e_frostbite.py` |
| `e2e_battleye.py` | drives a real `Bot` against `battleye.py` end to end | run it by hand |
| `e2e_frostbite.py` | the same for `frostbite.py` | run it by hand |

## Running one by hand

```bash
# a server on a random free port, printing everything it sees
python -m tools.fakeservers.battleye --password test
python -m tools.fakeservers.frostbite --password test

# the whole bot against one: connect, chat, a command, a ban, an unban
python -m tools.fakeservers.e2e_battleye
python -m tools.fakeservers.e2e_frostbite
```

## Using one in a test

Each server is a plain class with `start()`/`stop()` and an `address` — no fixtures, no framework:

```python
from tools.fakeservers.battleye import FakeBattleyeServer

server = FakeBattleyeServer(password="test")
server.start()
try:
    client = BattleyeClient(*server.address, "test")
    client.open()
    server.push("(Global) Bravo17: hello")
    assert client.read_lines() == ["(Global) Bravo17: hello"]
finally:
    server.stop()
```

## Adding one

Keep to the shape the BattlEye one sets:

- **one thread, one socket**, started by `start()` and joined by `stop()`, daemon so a failing test
  cannot hang the run;
- **bind to port 0** and expose the real `address`, so tests never collide or need a fixed port;
- **record what arrived** (`server.received`) so a test can assert on the wire, not just the effect;
- **let the test drive the server** — `push()` a message, script a reply — rather than simulating a
  whole game. A fake server that tries to *be* the game becomes a second thing to debug;
- **implement the protocol faithfully, including the awkward parts**. The value is in the parts that
  are easy to get wrong: this one really does require acknowledgements, really does resend an
  unacknowledged message, and really can split a reply across packets;
- **model state rather than canning replies** where the state is what makes a thing hard. The
  BattlEye fake keeps a real ban list, so `removeBan <index>` renumbers the remaining rows exactly as
  the game does — a fixed string would have hidden the very mistake worth catching. It also learns
  which GUID is in which slot from the identity messages it pushes, because a real server knows that,
  and a fake that forgot would let a wrong-slot ban pass.
