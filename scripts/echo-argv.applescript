-- Return the first argument unchanged.
-- The round-trip check: proves quotes, em dashes, accents and emoji survive
-- the stdin+argv path without interpolation.
on run argv
	if (count of argv) is 0 then return ""
	return item 1 of argv
end run
