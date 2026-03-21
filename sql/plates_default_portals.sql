-- Optional: set DB default for new rows when `portals` is omitted (e.g. API inserts).
-- Run in Supabase SQL editor if you want server-side defaults in addition to app inserts.

ALTER TABLE plates
  ALTER COLUMN portals SET DEFAULT ARRAY[
    'Boston (RMC Pay)',
    'New Bedford (RMC Pay)',
    'Lowell (RMC Pay)',
    'Brookline (RMC Pay)',
    'Chelsea (RMC Pay)',
    'Salem (RMC Pay)',
    'Quincy (RMC Pay)',
    'Salisbury (RMC Pay)',
    'Northampton (RMC Pay)',
    'Plymouth County (RMC Pay)'
  ]::text[];
