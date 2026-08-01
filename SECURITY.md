# Security policy

## Reporting a vulnerability

Report it privately through
[GitHub's advisory form](https://github.com/SirReaDy/BigBrotherBot/security/advisories/new), not as a
public issue. That keeps the detail out of sight until there is a fix to go with it.

Please say what you did, what happened, and which title and version you were running. A log excerpt
helps, but scrub it first: game logs and B3's own debug output contain player addresses, and a
`b3 probe` paste can contain an RCON password unless you passed `--redact`.

## What is in scope

B3 runs with the keys to a game server, so the things worth reporting are the ones that let somebody
who should not have those keys use them:

- Anything that lets a player reach a command above their level, or act on a player they cannot see.
- Anything that gets a crafted chat line, player name or log line executed, or that lets one reach
  RCON directly. Player-supplied values are meant to be sanitised before they reach a command; a
  case where they are not is a bug of this kind.
- Anything that discloses an RCON password, a database URL or an FTP credential, including through
  a log line, an error message or a `b3 probe` paste.
- Anything that makes a penalty land on the wrong player, or that lets a ban be evaded by changing
  name, slot or address.

## What is not

- **A game server that is itself insecure.** Most of the RCON protocols here are plain text over
  UDP, and some engines pass the password on every packet. That is the game's design, not this
  bot's; the mitigation is not to expose the RCON port to the internet.
- **Third-party plugins.** Anything installed with `b3 plugin install` is code from another
  repository, and installing it is a decision to trust that repository.
- **A misconfigured deployment.** A world-readable `b3.yaml` holding an RCON password is worth
  fixing, but the fix is file permissions.

## Supported versions

The `main` branch only. This is a rewrite in progress and there are no release branches to
backport to yet.
