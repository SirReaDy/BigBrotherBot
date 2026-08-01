"""Engine map id -> the name players use, for the six Frostbite titles.

A Frostbite server reports ``MP_Subway`` for the map everyone calls "Operation Metro". These tables
are what ``GameProfile.map_display`` consults so `!map`, `!maps` and `!nextmap` print a recognisable
name, and what `!map` matches against so an admin can type one.

Two shapes of key:

* **exact ids** on Frostbite 2 (BF3, BF4, Hardline, Warfighter), and
* **path prefixes** on Frostbite 1 (Bad Company 2, Medal of Honor), which report a level path with a
  gametype suffix. On Medal of Honor that suffix changes which map it is —
  ``levels/mp_01_elimination`` is Bagram Hanger where ``levels/mp_01`` is Mazar-i-Sharif Airfield —
  so the lookup takes the longest matching prefix and table order does not matter.

All keys are lowercase; the lookup lowercases the id the server reports.
"""

from __future__ import annotations

BF3_MAPS = {
    "mp_001": "Grand Bazaar",
    "mp_003": "Tehran Highway",
    "mp_007": "Caspian Border",
    "mp_011": "Seine Crossing",
    "mp_012": "Operation Firestorm",
    "mp_013": "Damavand Peak",
    "mp_017": "Noshahar Canals",
    "mp_018": "Kharg Island",
    "mp_subway": "Operation Metro",
    "xp1_001": "Strike At Karkand",
    "xp1_002": "Gulf of Oman",
    "xp1_003": "Sharqi Peninsula",
    "xp1_004": "Wake Island",
}

BF4_MAPS = {
    "mp_abandoned": "Zavod 311",
    "mp_damage": "Lancang Dam",
    "mp_flooded": "Flood Zone",
    "mp_journey": "Golmud Railway",
    "mp_naval": "Paracel Storm",
    "mp_prison": "Operation Locker",
    "mp_resort": "Hainan Resort",
    "mp_siege": "Siege of Shanghai",
    "mp_thedish": "Rogue Transmission",
    "mp_tremors": "Dawnbreaker",
    "xp0_caspian": "Caspian Border 2014",
    "xp0_firestorm": "Firestorm 2014",
    "xp0_oman": "Gulf Of Oman 2014",
    "xp0_metro": "Operation Metro 2014",
    "xp1_001": "Silk Road",
    "xp1_002": "Altai Range",
    "xp1_003": "Guilin Peaks",
    "xp1_004": "Dragon Pass",
    "xp2_001": "Lost Islands",
    "xp2_002": "Nansha Strike",
    "xp2_003": "Wave Breaker",
    "xp2_004": "Operation Mortar",
    "xp3_marketpl": "Pearl Market",
    "xp3_prpganda": "Propaganda",
    "xp3_urbangdn": "Lumphini Garden",
    "xp3_wtrfront": "Sunken Dragon",
    "xp4_arctic": "Operation Whiteout",
    "xp4_subbase": "Hammerhead",
    "xp4_titan": "Hangar 21",
    "xp4_wlkrftry": "Giants Of Karelia",
}

BFH_MAPS = {
    "mp_bank": "Bank Job",
    "mp_bloodout": "The Block",
    "mp_desert05": "Dust Bowl",
    "mp_downtown": "Downtown",
    "mp_eastside": "Derailed",
    "mp_glades": "Everglades",
    "mp_growhouse": "Growhouse",
    "mp_hills": "Hollywood Heights",
    "mp_offshore": "Riptide",
}

MOHW_MAPS = {
    "mp_03": "Somalia Stronghold",
    "mp_05": "Novi Grad Warzone",
    "mp_10": "Sarajevo Stadium",
    "mp_12": "Basilan Aftermath",
    "mp_13": "Hara Dunes",
    "mp_16": "Al Fara Cliffside",
    "mp_18": "Shogore Valley",
    "mp_19": "Tungunan Jungle",
    "mp_20": "Darra Gun Market",
    "mp_21": "Chitrail Compound",
}

#: Frostbite 1 reports a level path, matched on the longest prefix.
BFBC2_MAPS = {
    "levels/mp_001": "Panama Canal",
    "levels/mp_002": "Valparaiso",
    "levels/mp_003": "Laguna Alta",
    "levels/mp_004": "Isla Inocentes",
    "levels/mp_005": "Atacama Desert",
    "levels/mp_006": "Arica Harbor",
    "levels/mp_007": "White Pass",
    "levels/mp_008": "Nelson Bay",
    "levels/mp_009": "Laguna Preza",
    "levels/mp_012": "Port Valdez",
    "levels/bc1_oasis": "Oasis",
    "levels/bc1_harvest_day": "Harvest Day",
    "levels/mp_sp_002": "Cold War",
    "levels/mp_sp_005": "Heavy Metal",
    "levels/nam_mp_002": "Vantage Point",
    "levels/nam_mp_003": "Hill 137",
    "levels/nam_mp_005": "Cao Son Temple",
    "levels/nam_mp_006": "Phu Bai Valley",
}

#: The gametype suffix changes which map the path refers to on this title, so the suffixed entries
#: are distinct maps rather than duplicates.
MOH_MAPS = {
    "levels/mp_01_elimination": "Bagram Hanger",
    "levels/mp_01": "Mazar-i-Sharif Airfield",
    "levels/mp_02_koth": "Hindu Kush Pass",
    "levels/mp_02": "Shah-i-Knot Mountains",
    "levels/mp_03": "Khyber Caves",
    "levels/mp_04_koth": "Helmand River Hill",
    "levels/mp_04": "Helmand Valley",
    "levels/mp_05": "Kandahar Marketplace",
    "levels/mp_06": "Diwagal Camp",
    "levels/mp_07_koth": "Korengal Outpost",
    "levels/mp_08": "Kunar Base",
    "levels/mp_09": "Kabul City Ruins",
    "levels/mp_10": "Garmzir Town",
}
