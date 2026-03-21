-- Optional: set DB default for new rows when `portals` is omitted (e.g. API inserts).
-- Run in Supabase SQL editor if you want server-side defaults in addition to app inserts.

ALTER TABLE plates
  ALTER COLUMN portals SET DEFAULT ARRAY[
    'Boston (RMC Pay)',
    'New Bedford (RMC Pay)',
    'Lowell (RMC Pay)',
    'Brookline (RMC Pay)'
  ]::text[];
