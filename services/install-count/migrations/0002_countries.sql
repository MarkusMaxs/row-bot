-- Installations reporting on a UTC day, by country (D22). The country is the one Cloudflare works
-- out from the connection (request.cf.country, "XX" when unknown); each installation counts once a
-- day, and its country is never kept with its ID or its rows in pings. Kept, like totals.
CREATE TABLE countries (
  day TEXT NOT NULL,
  country TEXT NOT NULL,
  installs INTEGER NOT NULL,
  PRIMARY KEY (day, country)
) WITHOUT ROWID;
