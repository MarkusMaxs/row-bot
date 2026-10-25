-- One row per installation per UTC day. id_hash is an HMAC of the install ID (the key is the
-- ID_HMAC_KEY secret): never the ID itself, and never an IP address. Rows go after 35 days.
CREATE TABLE pings (
  id_hash TEXT NOT NULL,
  day TEXT NOT NULL,
  version TEXT NOT NULL,
  os TEXT NOT NULL,
  PRIMARY KEY (id_hash, day)
) WITHOUT ROWID;
CREATE INDEX pings_by_day ON pings (day);

-- Daily totals with no IDs: installations reporting in the last 7 and 15 days, overall
-- (kind 'all', name ''), by version and by OS. Kept.
CREATE TABLE totals (
  day TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('all', 'version', 'os')),
  name TEXT NOT NULL,
  active_7d INTEGER NOT NULL,
  active_15d INTEGER NOT NULL,
  PRIMARY KEY (day, kind, name)
) WITHOUT ROWID;
