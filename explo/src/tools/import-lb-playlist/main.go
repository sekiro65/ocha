package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"explo/src/discovery"
	"explo/src/util"
)

// Mirrors main.go's loadCustomTracks() cache schema exactly.
type cachedTrack struct {
	Title      string `json:"title"`
	Artist     string `json:"artist"`
	MainArtist string `json:"mainArtist"`
	Release    string `json:"release"`
	CoverURL   string `json:"coverUrl"`
	CoverPath  string `json:"coverPath"`
}

type cacheFile struct {
	Tracks []cachedTrack `json:"tracks"`
}

// Mirrors web/backend/playlist.CustomPlaylist exactly, so the dashboard
// (if you use it later) reads this the same way it'd read its own imports.
type customPlaylist struct {
	ID              string    `json:"id"`
	Name            string    `json:"name"`
	Source          string    `json:"source"`
	SourceURL       string    `json:"source_url,omitempty"`
	LBMBID          string    `json:"lb_mbid,omitempty"`
	ArtworkURL      string    `json:"artwork_url,omitempty"`
	ArtworkUploaded bool      `json:"artwork_uploaded,omitempty"`
	RefreshDays     int       `json:"refresh_days"`
	ColorIndex      int       `json:"color_index"`
	LastFetched     time.Time `json:"last_fetched"`
}

func extractMBID(input string) string {
	input = strings.TrimSpace(strings.TrimSuffix(input, "/"))
	parts := strings.Split(input, "/")
	return parts[len(parts)-1]
}

func main() {
	url := flag.String("url", "", "ListenBrainz playlist URL or raw MBID (required)")
	dataDir := flag.String("data-dir", "/opt/explo/config/", "Explo's WEB_DATA_PATH - must match what's in your .env")
	id := flag.String("id", "", "custom playlist id suffix (default: auto-generated from MBID)")
	flag.Parse()

	if *url == "" {
		fmt.Println("usage: import-lb-playlist -url <listenbrainz playlist URL or MBID> -data-dir <path>")
		os.Exit(1)
	}

	mbid := extractMBID(*url)
	playlistID := *id
	if playlistID == "" {
		playlistID = "custom-" + mbid[:8]
	} else if !strings.HasPrefix(playlistID, "custom-") {
		playlistID = "custom-" + playlistID
	}

	httpClient := util.NewHttp(util.HttpClientConfig{Timeout: 30})

	fmt.Printf("Fetching playlist %s ...\n", mbid)
	name, tracks, err := discovery.FetchPlaylistByMBID(httpClient, mbid)
	if err != nil {
		fmt.Printf("failed to fetch playlist: %s\n", err.Error())
		os.Exit(1)
	}
	fmt.Printf("Fetched %q - %d tracks\n", name, len(tracks))

	// --- write the cache file main.go's loadCustomTracks() actually reads ---
	cacheDir := filepath.Join(*dataDir, "cache")
	if err := os.MkdirAll(cacheDir, 0755); err != nil {
		fmt.Printf("failed to create cache dir: %s\n", err.Error())
		os.Exit(1)
	}

	cf := cacheFile{Tracks: make([]cachedTrack, len(tracks))}
	for i, t := range tracks {
		cf.Tracks[i] = cachedTrack{
			Title:      t.CleanTitle,
			Artist:     t.Artist,
			MainArtist: t.MainArtist,
			Release:    t.Album,
			CoverURL:   t.CoverURL,
			CoverPath:  t.CoverPath,
		}
	}
	cacheBytes, err := json.MarshalIndent(cf, "", "  ")
	if err != nil {
		fmt.Printf("failed to marshal cache: %s\n", err.Error())
		os.Exit(1)
	}
	cachePath := filepath.Join(cacheDir, playlistID+".json")
	if err := os.WriteFile(cachePath, cacheBytes, 0644); err != nil {
		fmt.Printf("failed to write cache file: %s\n", err.Error())
		os.Exit(1)
	}
	fmt.Printf("Wrote cache: %s\n", cachePath)

	// --- update custom-playlists.json metadata (append or replace by id) ---
	metaPath := filepath.Join(*dataDir, "custom-playlists.json")
	var playlists []customPlaylist
	if data, err := os.ReadFile(metaPath); err == nil {
		_ = json.Unmarshal(data, &playlists)
	}

	found := false
	for i, p := range playlists {
		if p.ID == playlistID {
			playlists[i].Name = name
			playlists[i].LastFetched = time.Now().UTC()
			found = true
			break
		}
	}
	if !found {
		playlists = append(playlists, customPlaylist{
			ID:          playlistID,
			Name:        name,
			Source:      "listenbrainz",
			SourceURL:   *url,
			LBMBID:      mbid,
			RefreshDays: 7,
			LastFetched: time.Now().UTC(),
		})
	}

	metaBytes, err := json.MarshalIndent(playlists, "", "  ")
	if err != nil {
		fmt.Printf("failed to marshal metadata: %s\n", err.Error())
		os.Exit(1)
	}
	if err := os.WriteFile(metaPath, metaBytes, 0644); err != nil {
		fmt.Printf("failed to write metadata: %s\n", err.Error())
		os.Exit(1)
	}
	fmt.Printf("Wrote metadata: %s\n", metaPath)

	fmt.Printf("\nDone. Run it with:\n  ./explo --playlist %s -d skip   # dry run first\n  ./explo --playlist %s           # real run\n", playlistID, playlistID)
}
