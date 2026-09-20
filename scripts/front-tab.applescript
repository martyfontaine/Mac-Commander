-- Return the title and URL of the active tab in the front browser window,
-- two lines: title, then URL. Optional argument 1 picks the browser
-- ("Vivaldi" or "Google Chrome"). With no argument, whichever is frontmost
-- wins, falling back to whichever is running. Never launches a browser.
on run argv
	set candidates to {"Vivaldi", "Google Chrome"}
	set browserName to ""

	if (count of argv) > 0 then
		-- Explicit choice: only accept a name from the list above.
		set requested to item 1 of argv
		if candidates contains requested then
			set browserName to requested
		else
			error "front-tab: unknown browser '" & requested & "'. Use Vivaldi or Google Chrome."
		end if
	else
		-- Prefer the browser that is frontmost right now.
		tell application "System Events"
			set frontName to name of first application process whose frontmost is true
		end tell
		if candidates contains frontName then
			set browserName to frontName
		else
			-- Neither is in front; take the first one that is running.
			repeat with c in candidates
				if application (c as text) is running then
					set browserName to c as text
					exit repeat
				end if
			end repeat
		end if
	end if

	if browserName is "" then error "front-tab: no supported browser is running."
	if not (application browserName is running) then error "front-tab: " & browserName & " is not running."

	-- Vivaldi and Chrome share Chrome's scripting dictionary.
	using terms from application "Google Chrome"
		tell application browserName
			if (count of windows) is 0 then error "front-tab: " & browserName & " has no windows open."
			set t to title of active tab of front window
			set u to URL of active tab of front window
		end tell
	end using terms from

	return t & linefeed & u
end run
