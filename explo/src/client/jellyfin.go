package client

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"log/slog"

	"explo/src/config"
	"explo/src/models"
	"explo/src/util"
)

type Paths []struct {
	Name           string   `json:"Name"`
	Locations      []string `json:"Locations"`
	CollectionType string   `json:"CollectionType"`
	ItemID         string   `json:"ItemId"`
	RefreshStatus  string   `json:"RefreshStatus"`
}

type Search struct {
	SearchHints      []SearchHints `json:"SearchHints"`
	TotalRecordCount int           `json:"TotalRecordCount"`
}
type SearchHints struct {
	ItemID      string `json:"ItemId"`
	ID          string `json:"Id"`
	Name        string `json:"Name"`
	Album       string `json:"Album"`
	AlbumID     string `json:"AlbumId"`
	AlbumArtist string `json:"AlbumArtist"`
}

type Audios struct {
	Items            []Items `json:"Items"`
	TotalRecordCount int     `json:"TotalRecordCount"`
	StartIndex       int     `json:"StartIndex"`
}

type ProviderIds struct {
	MusicBrainzTrack        string `json:"MusicBrainzTrack"`
}

type Items struct {
	Name        string      `json:"Name"`
	ServerID    string      `json:"ServerId"`
	ID          string      `json:"Id"`
	ProviderIds ProviderIds `json:"ProviderIds"`
	Path        string      `json:"Path"`
	Album       string      `json:"Album,omitempty"`
	AlbumArtist string      `json:"AlbumArtist,omitempty"`
	Artists     []string    `json:"Artists"`

}

type JFPlaylist struct {
	ID string `json:"Id"`
}

type JFUser struct {
	ID   string `json:"Id"`
	Name string `json:"Name"`
}

type Jellyfin struct {
	LibraryID  string
	HttpClient *util.HttpClient
	Cfg        config.ClientConfig
}

func NewJellyfin(cfg config.ClientConfig, httpClient *util.HttpClient) *Jellyfin {
	return &Jellyfin{Cfg: cfg,
		HttpClient: httpClient}
}

func (c *Jellyfin) AddHeader() error {
	if c.Cfg.Creds.Headers == nil {
		c.Cfg.Creds.Headers = make(map[string]string)
	}

	apiKey := c.resolveAPIKey()

	if apiKey == "" {
		return fmt.Errorf("API_KEY or ADMIN_API_KEY not set")
	}

	c.Cfg.Creds.Headers["Authorization"] = fmt.Sprintf("MediaBrowser Token=%s, Client=%s", apiKey, c.Cfg.ClientID)

	return nil
}

func (c *Jellyfin) GetAuth() error {
	return nil
}

func (c *Jellyfin) GetLibrary() error {
	reqParam := "/Library/VirtualFolders"

	body, err := c.HttpClient.MakeRequest("GET", c.Cfg.URL+reqParam, nil, c.Cfg.Creds.Headers)
	if err != nil {
		return err
	}

	var paths Paths
	if err = util.ParseResp(body, &paths); err != nil {
		return err
	}

	for _, path := range paths {
		if path.Name == c.Cfg.LibraryName {
			c.LibraryID = path.ItemID
			return nil
		}
	}
	return fmt.Errorf("failed to find library named %s", c.Cfg.LibraryName)
}

func (c *Jellyfin) AddLibrary() error {
	cleanPath := url.PathEscape(c.Cfg.DownloadDir)
	reqParam := fmt.Sprintf("/Library/VirtualFolders?name=%s&paths=%s&collectionType=music&refreshLibrary=true", c.Cfg.LibraryName, cleanPath)
	payload := []byte(`{
		"LibraryOptions": {
		  "Enabled": true,
		  "EnableRealtimeMonitor": true,
		  "EnableLUFSScan": false
		}
	  }`)

	if _, err := c.HttpClient.MakeRequest("POST", c.Cfg.URL+reqParam, bytes.NewReader(payload), c.Cfg.Creds.Headers); err != nil {
		return fmt.Errorf("failed to add library to Jellyfin using the download path, please define a library name using LIBRARY_NAME in .env: %s", err.Error())
	}
	return nil
}

func (c *Jellyfin) RefreshLibrary() error {
	reqParam := fmt.Sprintf("/Items/%s/Refresh?metadataRefreshMode=FullRefresh&Recursive=true", c.LibraryID)

	if _, err := c.HttpClient.MakeRequest("POST", c.Cfg.URL+reqParam, nil, c.Cfg.Creds.Headers); err != nil {
		return err
	}
	return nil
}

func (c *Jellyfin) CheckRefreshState() bool {
	return false
}

// splitArtists breaks a compound artist credit string (as ListenBrainz often
// returns for regional/collaborative tracks, e.g. "Rzee, Chinmayi Kiranjal,
// Minya Panicker") into individual names for matching against a library
// file's own (usually single-artist) tags.
func splitArtists(s string) []string {
	replacer := strings.NewReplacer(
		" feat. ", ",", " feat ", ",", " ft. ", ",", " ft ", ",",
		" with ", ",", " x ", ",", " & ", ",",
	)
	normalized := replacer.Replace(" " + strings.ToLower(s) + " ")
	parts := strings.Split(normalized, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if trimmed := strings.TrimSpace(p); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

// stripTitleSuffix removes common regional-metadata title decorations
// (film-name attributions, remix/version tags) that ListenBrainz includes
// but a locally-tagged file usually doesn't, e.g. turning
// `Engotta - From "Balan - The Boy"` into `Engotta`, or
// `KALYANI (with Shreya Ghoshal) - Remix` into `KALYANI`.
// withFeatRe removes ONLY a "(with X)" / "(feat X)" / "(ft X)" collaborator
// clause - a surgical removal, not a truncate-to-end-of-string, so a
// trailing variant marker like "- Remix" that comes AFTER the clause is
// preserved and still available for comparison on both sides.
var withFeatRe = regexp.MustCompile(`(?i)\s*\((?:with|feat\.?|ft\.?)\s+[^)]*\)`)

// fromFilmRe removes trailing film/source attribution (e.g. 'From "Movie"', '- From Movie', '(From Movie)', 'From _Movie_')
var fromFilmRe = regexp.MustCompile(`(?i)\s*(?:[-(\[]\s*)?from\s+.*$`)

// versionSuffixRe removes common audio version descriptors in brackets/parentheses
var versionSuffixRe = regexp.MustCompile(`(?i)\s*[\(\[](?:radio edit|edit|remaster(?:ed)?(?:\s*\d+)?|acoustic|live|bonus track|official video|video|single version|album version)[\)\]]`)

func stripTitleSuffix(title string) string {
	stripped := withFeatRe.ReplaceAllString(title, "")
	stripped = fromFilmRe.ReplaceAllString(stripped, "")
	stripped = versionSuffixRe.ReplaceAllString(stripped, "")
	stripped = strings.TrimSuffix(strings.TrimSpace(stripped), "-")
	return strings.TrimSpace(stripped)
}

// baseTitleRe cuts a title at the first "(", "[" or " - ", e.g.
// `My Heart Will Go On (love theme from "Titanic")` -> `My Heart Will Go On`.
// Only used as a last resort when the artist already matches, so a missed
// suffix pattern can't turn into a daily re-download loop.
var baseTitleRe = regexp.MustCompile(`\s*(?:[(\[]| - ).*$`)

func baseTitle(title string) string {
	return strings.TrimSpace(baseTitleRe.ReplaceAllString(title, ""))
}

func levenshtein(a, b string) int {
	if a == b {
		return 0
	}
	la, lb := len(a), len(b)
	if la == 0 {
		return lb
	}
	if lb == 0 {
		return la
	}
	prev := make([]int, lb+1)
	curr := make([]int, lb+1)
	for j := 0; j <= lb; j++ {
		prev[j] = j
	}
	for i := 1; i <= la; i++ {
		curr[0] = i
		for j := 1; j <= lb; j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			del := prev[j] + 1
			ins := curr[j-1] + 1
			sub := prev[j-1] + cost
			min := del
			if ins < min {
				min = ins
			}
			if sub < min {
				min = sub
			}
			curr[j] = min
		}
		prev, curr = curr, prev
	}
	return prev[lb]
}

// titleSimilarity returns a 0..1 ratio (1 = identical) - used as a fallback
// when exact normalized comparison fails, specifically to catch regional
// transliteration variance ListenBrainz vs beets/MusicBrainz disagree on
// (e.g. "Shyaamaambaram" vs "Shyamambaram", "Etho Saayaana" vs "Etho
// Sayana" - doubled vowels are extremely common in Malayalam/Hindi
// romanization and neither side is "wrong", they're just different
// transliteration conventions).
func titleSimilarity(a, b string) float64 {
	maxLen := len(a)
	if len(b) > maxLen {
		maxLen = len(b)
	}
	if maxLen == 0 {
		return 1
	}
	dist := levenshtein(a, b)
	return 1 - float64(dist)/float64(maxLen)
}

// fuzzyTitleThreshold: how similar two normalized titles must be (0..1) to
// count as a match when nothing compared exactly. Tuned against real
// examples: "shyamambaram" vs "shyaamaambaram" (~0.83 similarity, doubled
// vowels) needs to pass; unrelated songs sharing only a short common
// fragment need to fail. 0.80 gives headroom for one or two extra/missing
// vowels on a typical title length while staying strict on short titles.
const fuzzyTitleThreshold = 0.80

func (c *Jellyfin) SearchSongs(tracks []*models.Track) error {
	// Fetch the ENTIRE audio library once, rather than one SearchTerm query
	// per track. Jellyfin's search does substring matching against indexed
	// text - a transliteration variant ("Saayaana" vs "Sayana") simply won't
	// surface as a candidate via SearchTerm at all, no matter how good the
	// comparison logic below is. Fetching everything and matching in-memory
	// sidesteps that entirely, and for a library this size it's actually
	// FEWER network calls than doing it per-track.
	reqParam := "/Items?IncludeMediaTypes=Audio&Recursive=true&Fields=Path,ProviderIDs&Limit=10000"
	body, err := c.HttpClient.MakeRequest("GET", c.Cfg.URL+reqParam, nil, c.Cfg.Creds.Headers)
	if err != nil {
		return err
	}
	var results Audios
	if err = util.ParseResp(body, &results); err != nil {
		return err
	}
	slog.Info("[jellyfin-debug] library fetch", "items_returned", len(results.Items))

	debugCount := 0
	for _, track := range tracks {
		normalizedCleanTitle := util.NormalizeTitle(track.CleanTitle)
		normalizedStrippedTitle := util.NormalizeTitle(stripTitleSuffix(track.CleanTitle))
		wantedArtists := splitArtists(track.MainArtist)

		if debugCount < 3 {
			slog.Info("[jellyfin-debug] track", "cleanTitle", track.CleanTitle, "mainArtist", track.MainArtist,
				"normalizedCleanTitle", normalizedCleanTitle, "normalizedStrippedTitle", normalizedStrippedTitle,
				"wantedArtists", wantedArtists)
		}

		bestSim := -1.0
		var bestItem Items

		for _, item := range results.Items {

			normalizedItemTitle := util.NormalizeTitle(item.Name)
			normalizedItemStripped := util.NormalizeTitle(stripTitleSuffix(item.Name))

			if debugCount < 3 {
				sim := titleSimilarity(normalizedStrippedTitle, normalizedItemStripped)
				if sim > bestSim {
					bestSim = sim
					bestItem = item
				}
			}

			musicBrainzMatch := track.MusicBrainzTrackID != "" && item.ProviderIds.MusicBrainzTrack == track.MusicBrainzTrackID
			titleMatch := normalizedItemTitle == normalizedCleanTitle ||
				normalizedItemStripped == normalizedStrippedTitle ||
				normalizedItemTitle == normalizedStrippedTitle ||
				normalizedItemStripped == normalizedCleanTitle

			// Original strict check: exact match against AlbumArtist or the
			// first listed artist.
			artistMatch := strings.EqualFold(item.AlbumArtist, track.MainArtist) || (len(item.Artists) > 0 && strings.EqualFold(item.Artists[0], track.MainArtist))

			// Loosened fallback: does ANY individual artist from a split
			// compound credit ("A, B, C") appear (as a substring, either
			// direction) in the item's AlbumArtist or any of its Artists?
			// Catches cases where ListenBrainz lists 5 collaborators but the
			// file is only tagged with the lead artist, or vice versa.
			if !artistMatch {
				itemArtistFields := append([]string{item.AlbumArtist}, item.Artists...)
				for _, wanted := range wantedArtists {
					if wanted == "" {
						continue
					}
					for _, itemArtist := range itemArtistFields {
						itemArtistLower := strings.ToLower(itemArtist)
						if itemArtistLower == "" {
							continue
						}
						if strings.Contains(itemArtistLower, wanted) || strings.Contains(wanted, itemArtistLower) {
							artistMatch = true
							break
						}
					}
					if artistMatch {
						break
					}
				}
			}

			// Fuzzy fallback: catches transliteration variance ("Shyamambaram"
			// vs "Shyaamaambaram") that no exact/stripped comparison can, but
			// ONLY when an artist already agrees - title similarity alone on a
			// short/generic title could otherwise false-positive against an
			// unrelated song.
			if !titleMatch && artistMatch {
				if titleSimilarity(normalizedStrippedTitle, normalizedItemStripped) >= fuzzyTitleThreshold {
					titleMatch = true
				}
			}

			if !titleMatch && artistMatch {
				if b := util.NormalizeTitle(baseTitle(track.CleanTitle)); b != "" && b == util.NormalizeTitle(baseTitle(item.Name)) {
					titleMatch = true
				}
			}

			pathMatch := util.ContainsFold(item.Path, track.File)

			if musicBrainzMatch || (titleMatch && artistMatch) {
				track.ID = item.ID
				track.Present = true
				break
			}

			if track.File != "" && artistMatch && pathMatch {
				track.ID = item.ID
				track.Present = true
				break
			}
		}

		if debugCount < 3 {
			bestArtistMatch := strings.EqualFold(bestItem.AlbumArtist, track.MainArtist) ||
				(len(bestItem.Artists) > 0 && strings.EqualFold(bestItem.Artists[0], track.MainArtist))
			slog.Info("[jellyfin-debug] best candidate", "wantedTitle", track.CleanTitle, "wantedArtist", track.MainArtist,
				"bestItemName", bestItem.Name, "bestItemAlbumArtist", bestItem.AlbumArtist, "bestItemArtists", bestItem.Artists,
				"similarity", bestSim, "strictArtistMatch", bestArtistMatch, "present", track.Present)
			debugCount++
		}

		if !track.Present {
			slog.Debug(fmt.Sprintf("[jellyfin] failed to find '%s' by '%s' in album '%s'", track.Title, track.Artist, track.Album))
		}
	}
	return nil
}

func (c *Jellyfin) SearchPlaylist() error {
	queryParams := fmt.Sprintf("/Search/Hints?IncludeItemTypes=Playlist&SearchTerm=%s", url.QueryEscape(c.Cfg.PlaylistName))
	body, err := c.HttpClient.MakeRequest("GET", c.Cfg.URL+queryParams, nil, c.Cfg.Creds.Headers)
	if err != nil {
		return err
	}

	var results Search
	if err = util.ParseResp(body, &results); err != nil {
		return err
	}

	if len(results.SearchHints) != 0 {
		c.Cfg.PlaylistID = results.SearchHints[0].ID
		return nil
	} else {
		return fmt.Errorf("no results found for playlist: %s", c.Cfg.PlaylistName)
	}
}

func (c *Jellyfin) CreatePlaylist(tracks []*models.Track) error {

	songs, err := formatJFSongs(tracks)
	if err != nil {
		return fmt.Errorf("failed to marshal track IDs: %s", err.Error())
	}
	var userID string
	isPublic := c.Cfg.PublicPlaylist

	if c.Cfg.Creds.User != "" {
		userID, err = c.ResolveUserID()
		if err != nil {
			return err
		}
	} else {
		userID = c.resolveAPIKey()
		isPublic = true
	}


	queryParams := "/Playlists"
	payload := fmt.Appendf(nil, `
		{
		"Name": "%s",
		"Ids": %s,
		"MediaType": "Audio",
		"UserId": "%s",
		"IsPublic": %t
		}`, c.Cfg.PlaylistName, songs, userID, isPublic)

	body, err := c.HttpClient.MakeRequest("POST", c.Cfg.URL+queryParams, bytes.NewReader(payload), c.Cfg.Creds.Headers)
	if err != nil {
		return err
	}
	var playlist JFPlaylist
	if err = util.ParseResp(body, &playlist); err != nil {
		return err
	}
	c.Cfg.PlaylistID = playlist.ID
	return nil
}

func (c *Jellyfin) UpdatePlaylist() error {
	isPublic := c.Cfg.PublicPlaylist
	if c.Cfg.Creds.User == "" {
		isPublic = true
	}
	queryParams := fmt.Sprintf("/Items/%s", c.Cfg.PlaylistID)
	payload := fmt.Appendf(nil, `
		{
		"Id":"%s",
		"Name":"%s",
		"Overview":"%s",
		"IsPublic":%t,
		"Genres":[],
		"Tags":[],
		"ProviderIds":{}
		}`, c.Cfg.PlaylistID, c.Cfg.PlaylistName, c.Cfg.PlaylistDescr, isPublic) // the additional fields have to be added, otherwise JF returns code 400

	if _, err := c.HttpClient.MakeRequest("POST", c.Cfg.URL+queryParams, bytes.NewBuffer(payload), c.Cfg.Creds.Headers); err != nil {
		return err
	}
	return nil
}

func (c *Jellyfin) DeletePlaylist() error {
	queryParams := fmt.Sprintf("/Items/%s", c.Cfg.PlaylistID)

	if _, err := c.HttpClient.MakeRequest("DELETE", c.Cfg.URL+queryParams, nil, c.Cfg.Creds.Headers); err != nil {
		return fmt.Errorf("deleyeJfPlaylist(): %s", err.Error())
	}
	return nil
}

// SetPlaylistArtwork uploads a JPEG as the playlist's primary image.
func (c *Jellyfin) SetPlaylistArtwork(localPath string) error {
	if c.Cfg.PlaylistID == "" {
		return fmt.Errorf("jellyfin: no PlaylistID set")
	}
	return uploadPlaylistArtwork(c.HttpClient, c.Cfg.URL+"/Items/"+c.Cfg.PlaylistID+"/Images/Primary", localPath, c.Cfg.Creds.Headers)
}

func formatJFSongs(tracks []*models.Track) ([]byte, error) { // marshal track IDs
	songIDs := make([]string, 0, len(tracks))
	for _, track := range tracks {
		if track.Present {
			songIDs = append(songIDs, track.ID)
		}
	}
	songs, err := json.Marshal(songIDs)
	if err != nil {
		return nil, err
	}
	return songs, nil
}

func (c *Jellyfin) ResolveUserID() (string, error) {
	body, err := c.HttpClient.MakeRequest("GET", c.Cfg.URL+"/Users", nil, c.Cfg.Creds.Headers)
	if err != nil {
		return "", err
	}
	var users []JFUser
	if err = util.ParseResp(body, &users); err != nil {
		return "", err
	}
	for _, user := range users {
		if strings.EqualFold(user.Name, c.Cfg.Creds.User) {
			return user.ID, nil
		}
	}

	return "", fmt.Errorf("failed to find Jellyfin user %q", c.Cfg.Creds.User)
}

// Check which API Key variable is used
func (c *Jellyfin) resolveAPIKey() string {
	if c.Cfg.AdminCreds.APIKey != "" {
		return c.Cfg.AdminCreds.APIKey
	}
	return c.Cfg.Creds.APIKey
}