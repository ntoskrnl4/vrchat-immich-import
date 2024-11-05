import hashlib
import json
import os
import struct
from collections import defaultdict

import requests
from datetime import datetime
from pathlib import Path

api_key = {"X-Api-Key": ""}  # place your API key here (Immich Account Settings >  API Keys)
server = "https://your.immich.instance/api"  # URL to your instance with /api on the end

vrc_album_id = ""  # Optional: Script will place photos into this album on upload (UUID) (must already exist)
vrc_tag_id = ""  # Optional: Same with this tag
vrc_uid_mapping = {
    # Optional: Mapping from VRChat id to Immich tag UUID (the tag must already exist)
    # There needs to be a mapping for both the username AND the usr_ID, because VRCX doesn't save usr_ID on non-friends
}

unknown_users = {}

"""
We need to:
- Check if assets exists (by hex-sha1 and filename) > POST /api/assets/bulk-upload-check
- Filter out ones that already exist on server
- Read file contents to check for iTXtDescription section
    - If found, parse contents
    - Convert user IDs to Immich tags
- Upload files one by one, w/ tags, into VRChat album
"""


def filter_out_assets(response):
    for item in response["results"]:
        if item["action"] == "reject":
            filter_out.append(item["id"])
        elif item["action"] != "accept":
            # Actions should only be "accept" or "reject"?
            breakpoint()


def parse_player_metadata(file: Path, assetid: str):
    global unknown_users
    with file.open("rb") as f:
        contents = f.read()

    try:
        meta_loc = contents.index(b"iTXtDescription")
    except ValueError:
        return  # If no metadata, skip file

    meta_len = struct.unpack(">I", contents[meta_loc-4:meta_loc])[0] - 16
    meta_loc += 4 + 0x10  # skip over the PNG chunk header iTXt and EXIF tag name b"Description\0\0\0\0\0"
    info = json.loads(contents[meta_loc:meta_loc+meta_len].decode("utf-8"))
    del contents  # please get rid of this thing eating 2093485672 MB of memory aaaahhhhh
    
    if info["application"] != "VRCX" or info["version"] != 1:
        return
    
    for player in info["players"]:
        usr_id, name = player["id"], player["displayName"]
        if usr_id in vrc_uid_mapping:
            player_tags[vrc_uid_mapping[usr_id]].add(assetid)
            continue
        if name in vrc_uid_mapping:
            player_tags[vrc_uid_mapping[name]].add(assetid)
            continue
        unknown_users[name] = usr_id

    
if __name__ == '__main__':

    # Get list of all local screenshots
    files = []
    for sub in Path(".").iterdir():
        
        # Some checks to make sure we only get directories that look like year-month's
        if not sub.is_dir() \
        or not (sub.name[4] == "-") \
        or not (1 <= int(sub.name[5:7]) <= 12) \
        or not (2014 <= int(sub.name[0:4]) <= 2099):
            print(f"[ignore] {sub.__str__()}")
            continue
    
        files.extend(sub.iterdir())

    print(f"Found {len(files)} files in screenshot folders")


    # Great, let's move on to hashing+filtering everything now
    data = {"assets": []}
    filter_out = []
    print("Hashing and filtering, this may take a moment... (i hope this is an SSD)")

    for file in files:
        with file.open("rb") as f:
            data["assets"].append(
                    {
                        "id": file.name,
                        "checksum": hashlib.file_digest(f, "sha1").hexdigest()
                    }
            )

        if len(data["assets"]) >= 50:
            response = requests.post(server+"/assets/bulk-upload-check", headers=api_key, json=data)
            if response.status_code != 200:
                breakpoint()
            filter_out_assets(response.json())
            data["assets"] = []

    # repeat that again down here once we break out of the loop
    if data["assets"]:
        response = requests.post(server+"/assets/bulk-upload-check", headers=api_key, json=data)
        if response.status_code != 200:
            breakpoint()
        filter_out_assets(response.json())
    data["assets"] = []

    # Now apply the filter to our base list
    files = [x for x in files if x.name not in filter_out]
    print(f"{len(files)} after filtering - now processing and uploading assets")
    
    # Sweet, now we can iterate over our files and process+upload each one
    new_asset_ids = []  # We can bulk-add everything to the vrchat album later on
    # Mapping of tag IDs to a list of assets to add
    player_tags = defaultdict(set)

    for file in files:
        stats = file.stat()
        data = {
            "deviceAssetId": f"{file.name}-{stats.st_mtime}",
            "deviceId": "python/vrchat_sync_script",
            "fileCreatedAt": datetime.fromtimestamp(stats.st_mtime).isoformat(),
            "fileModifiedAt": datetime.fromtimestamp(stats.st_mtime).isoformat(),
            "isFavorite": "false",
        }
        with file.open("rb") as f:
            response = requests.post(server+"/assets", headers=api_key, data=data, files={"assetData": f})

        if response.status_code not in [200, 201]:
            breakpoint()

        new_asset_ids.append(response.json()["id"])
        parse_player_metadata(file, response.json()["id"])
        # parse_player_metadata(file, "test")
        print(f"\t\tNEW -> {file.__str__()}")

    if vrchat_album_id:  # If not used, don't try this as it'll just error
        r = requests.put(server+f"/albums/{vrc_album_id}/assets", headers=api_key, json={"ids": new_asset_ids})
        if r.status_code not in [200, 201]:
            breakpoint()

    if vrc_tag_id:  # If not used, don't try this as it'll just error
        r = requests.put(server+f"/tags/{vrc_tag_id}/assets", headers=api_key, json={"ids": new_asset_ids})
        if r.status_code not in [200, 201]:
            breakpoint()

    for tag, assets in player_tags.items():
        r = requests.put(server+f"/tags/{tag}/assets", headers=api_key, json={"ids": list(assets)})
        if r.status_code not in [200, 201]:
            breakpoint()

    if vrc_uid_mapping:  # If not used, don't complain about unmatched/untagged users
        print("The following usernames/IDs were not found in the tag mapping:")
        for uname, uid in unknown_users.items():
            print(f"\t\t{uid if uid else '<unknown usr_ID>'}: {uname}")

    print("Done")
