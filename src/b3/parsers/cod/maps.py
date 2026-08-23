"""Engine map id -> the name players use, for the Call of Duty titles that need a table.

Only Black Ops does. Every other Call of Duty title here reports a map id an admin recognises
(`mp_crossfire`, `mp_vacant`), but Black Ops ships a set whose ids and names diverge — `mp_nuked` is
Nuketown, `mp_duga` is Grid, `mp_area51` is Hangar 18 — so an admin reading a rotation off `!maps`
sees ids nobody uses in conversation.

This is what `GameProfile.map_display` consults, so `!maps`, `!nextmap` and `callvote`'s
announcements print a recognisable name, and what `!map` matches against so an admin can type one.
It is also the list `poweradmincod7` does arithmetic on: a **ranked** Black Ops server cannot be told
to load a map, only which maps to exclude, so "set the next map" there means "exclude the other
twenty-five" — and that needs the full stock set, not the rotation the server reports.

All keys are lowercase; the lookup lowercases the id the server reports.
"""

from __future__ import annotations

#: The 26 stock Black Ops multiplayer maps: the sixteen on the disc and the ten from the four map
#: packs, in the order Treyarch released them. The map packs matter to `!pasetdlc`, which switches a
#: pack on or off by number.
COD7_MAPS = {
    # On the disc.
    "mp_array": "Array",
    "mp_cairo": "Havana",
    "mp_cosmodrome": "Launch",
    "mp_cracked": "Cracked",
    "mp_crisis": "Crisis",
    "mp_duga": "Grid",
    "mp_firingrange": "Firing Range",
    "mp_hanoi": "Hanoi",
    "mp_havoc": "Jungle",
    "mp_mountain": "Summit",
    "mp_nuked": "Nuketown",
    "mp_radiation": "Radiation",
    "mp_russianbase": "WMD",
    "mp_villa": "Villa",
    # First Strike.
    "mp_berlinwall2": "Berlin Wall",
    "mp_discovery": "Discovery",
    "mp_kowloon": "Kowloon",
    "mp_stadium": "Stadium",
    # Escalation.
    "mp_gridlock": "Convoy",
    "mp_hotel": "Hotel",
    "mp_outskirts": "Stockpile",
    "mp_zoo": "Zoo",
    # Annihilation.
    "mp_area51": "Hangar 18",
    "mp_drivein": "Drive-In",
    "mp_silo": "Silo",
    # Rezurrection.
    "mp_golfcourse": "Hazard",
}

__all__ = ["COD7_MAPS"]
