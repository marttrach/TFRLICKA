"""Refresh the local TDX station cache using environment credentials.

Also reports how many stations got a county, because the county picker is
unusable without it and a silent zero looks identical to a working refresh.
"""

from tra_sniper.tdx import TdxClient


def main() -> None:
    client = TdxClient()
    stations = client.fetch_stations()
    resolved = sum(1 for station in stations if station["county"])

    print(f"Saved {len(stations)} stations to {client.station_cache_path}")
    print(f"County resolved for {resolved}/{len(stations)} stations")

    if resolved:
        counties = sorted({station["county"] for station in stations if station["county"]})
        print(f"Counties: {'、'.join(counties)}")
        return

    # Nothing resolved: the field names this code reads are not the ones TDX
    # actually returns. Print the real ones instead of guessing again.
    print("\nNo county could be read from any station.")
    records = client._records(client._get("Station"), "Stations")
    if records:
        print("Fields TDX actually returned for the first station:")
        for key in sorted(records[0]):
            print(f"  {key} = {records[0][key]!r}")
        print("\nUpdate _county_of() in src/tra_sniper/tdx.py to read the right field.")


if __name__ == "__main__":
    main()
