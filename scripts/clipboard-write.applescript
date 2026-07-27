-- Set the clipboard to argument 1.
on run argv
	if (count of argv) is 0 then error "clipboard-write needs one argument"
	set the clipboard to item 1 of argv
	return "ok"
end run
