package downloader

import (
	"os"
	"testing"

	"explo/src/config"
	"explo/src/models"
	"explo/src/util"
)

// Run with:
//   cd ~/Explo
//   go test ./src/downloader/ -run TestSpotifyResolve -v
//
// Requires SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET to already be set in
// your shell environment (source your .env first, or export them manually).
// This does NOT touch ListenBrainz, Jellyfin, or the Telegram bot - it only
// exercises the Spotify Client Credentials auth + search call, using a
// well-known track (title+artist fallback path, no ISRC needed) so there's
// nothing to guess wrong.
func TestSpotifyResolve(t *testing.T) {
	cfg := config.Spotify{
		ClientID:     mustEnv(t, "SPOTIFY_CLIENT_ID"),
		ClientSecret: mustEnv(t, "SPOTIFY_CLIENT_SECRET"),
	}

	httpClient := util.NewHttp(util.HttpClientConfig{Timeout: 20})
	resolver := newSpotifyResolver(cfg, httpClient)

	track := &models.Track{
		CleanTitle: "Bohemian Rhapsody",
		MainArtist: "Queen",
	}

	link, err := resolver.ResolveTrack(track)
	if err != nil {
		t.Fatalf("resolution failed: %v", err)
	}

	t.Logf("Resolved Spotify link: %s", link)

	if link == "" {
		t.Fatal("got empty link with no error - unexpected")
	}
}

func mustEnv(t *testing.T, key string) string {
	v := os.Getenv(key)
	if v == "" {
		t.Fatalf("%s is not set in the environment - export it or source your .env first", key)
	}
	return v
}
