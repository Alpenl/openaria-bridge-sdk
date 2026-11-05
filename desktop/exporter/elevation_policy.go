package main

// elevationDecision is shared with tests so all startup states are exercised
// without invoking UAC. 0 continues, 1 relaunches, -1 refuses a loop.
func elevationDecision(elevated, marked bool) int {
	if elevated {
		return 0
	}
	if marked {
		return -1
	}
	return 1
}
