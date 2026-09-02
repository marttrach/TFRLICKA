"""Refresh the local TDX station cache using environment credentials."""

from tra_sniper.tdx import TdxClient


def main() -> None:
    client = TdxClient()
    stations = client.fetch_stations()
    print(f"Saved {len(stations)} stations to {client.station_cache_path}")


if __name__ == "__main__":
    main()
