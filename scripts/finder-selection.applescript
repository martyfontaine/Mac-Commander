-- Return the POSIX paths of the current Finder selection, one per line.
on run argv
	tell application "Finder"
		set sel to selection as alias list
		set out to {}
		repeat with anItem in sel
			set end of out to POSIX path of (anItem as text)
		end repeat
	end tell
	set text item delimiters to linefeed
	return out as text
end run
