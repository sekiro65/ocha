package client

import "testing"

func TestBaseTitle(t *testing.T) {
	cases := map[string]string{
		"My Heart Will Go On (love theme from “Titanic”)": "My Heart Will Go On",
		"KALYANI (with Shreya Ghoshal) - Remix":            "KALYANI",
		"Engotta - From \"Balan - The Boy\"":               "Engotta",
		"Incomplete":                                       "Incomplete",
	}
	for in, want := range cases {
		if got := baseTitle(in); got != want {
			t.Errorf("baseTitle(%q) = %q, want %q", in, got, want)
		}
	}
}
