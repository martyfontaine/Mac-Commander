-- Return the clipboard as text, or "" if it holds something non-textual.
on run argv
	try
		return (the clipboard as text)
	on error
		return ""
	end try
end run
