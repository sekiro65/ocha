package downloader

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"explo/src/config"
	"explo/src/models"
	"explo/src/util"

	"golang.org/x/time/rate"
)

// ── Spotify resolution (Client Credentials, app-only auth) ─────────────────

type spotifyToken struct {
	AccessToken string    `json:"access_token"`
	ExpiresIn   int       `json:"expires_in"`
	fetchedAt   time.Time `json:"-"`
}

type spotifySearchResp struct {
	Tracks struct {
		Items []struct {
			ID         string `json:"id"`
			ExternalUR struct {
				Spotify string `json:"spotify"`
			} `json:"external_urls"`
		} `json:"items"`
	} `json:"tracks"`
}

type spotifyResolver struct {
	cfg    config.Spotify
	http   *util.HttpClient
	mu     sync.Mutex
	token  *spotifyToken
}

func newSpotifyResolver(cfg config.Spotify, httpClient *util.HttpClient) *spotifyResolver {
	return &spotifyResolver{cfg: cfg, http: httpClient}
}

func (s *spotifyResolver) getToken() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.token != nil && time.Since(s.token.fetchedAt) < time.Duration(s.token.ExpiresIn-30)*time.Second {
		return s.token.AccessToken, nil
	}

	form := url.Values{}
	form.Set("grant_type", "client_credentials")
	form.Set("client_id", s.cfg.ClientID)
	form.Set("client_secret", s.cfg.ClientSecret)

	// NOTE: deliberately not using s.http.MakeRequest here - it unconditionally
	// adds Content-Type: application/json via req.Header.Add() *before*
	// applying caller headers, so passing our own Content-Type just gets
	// appended alongside it rather than replacing it. Spotify's token
	// endpoint rejects the resulting dual-Content-Type request with a 400.
	// Building this one request directly avoids that collision.
	req, err := http.NewRequest("POST", "https://accounts.spotify.com/api/token", strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("failed to build spotify token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := s.http.Client.Do(req)
	if err != nil {
		return "", fmt.Errorf("spotify auth request failed: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("failed to read spotify auth response: %w", err)
	}
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("spotify auth failed: got %d from token endpoint: %s", resp.StatusCode, string(respBody))
	}

	var tok spotifyToken
	if err := json.Unmarshal(respBody, &tok); err != nil {
		return "", fmt.Errorf("failed to parse spotify token response: %w", err)
	}
	tok.fetchedAt = time.Now()
	s.token = &tok
	return tok.AccessToken, nil
}

// ResolveTrack returns a Spotify track URL for the given track, preferring
// ISRC lookup (exact match) and falling back to title+artist search.
func (s *spotifyResolver) ResolveTrack(track *models.Track) (string, error) {
	tok, err := s.getToken()
	if err != nil {
		return "", err
	}
	headers := map[string]string{"Authorization": "Bearer " + tok}

	// Prefer ISRC — exact, no fuzzy matching involved
	if len(track.ISRCs) > 0 {
		for _, isrc := range track.ISRCs {
			q := url.QueryEscape(fmt.Sprintf("isrc:%s", isrc))
			link, err := s.search(q, headers)
			if err == nil && link != "" {
				return link, nil
			}
		}
	}

	// Fallback: title + artist text search
	q := url.QueryEscape(fmt.Sprintf("track:%s artist:%s", track.CleanTitle, track.MainArtist))
	return s.search(q, headers)
}

func (s *spotifyResolver) search(q string, headers map[string]string) (string, error) {
	reqURL := fmt.Sprintf("https://api.spotify.com/v1/search?q=%s&type=track&limit=1", q)
	body, err := s.http.MakeRequest("GET", reqURL, nil, headers)
	if err != nil {
		return "", err
	}
	var res spotifySearchResp
	if err := util.ParseResp(body, &res); err != nil {
		return "", err
	}
	if len(res.Tracks.Items) == 0 {
		return "", fmt.Errorf("no spotify match found")
	}
	return res.Tracks.Items[0].ExternalUR.Spotify, nil
}

// ── Telegram downloader backend ─────────────────────────────────────────────

type Telegram struct {
	Cfg         config.Telegram
	HttpClient  *util.HttpClient
	DownloadDir string
	Spotify     *spotifyResolver
	// Throttles how often GetTrack actually dispatches a request to the
	// music-giving bot, independent of Explo's global DOWNLOAD_LIMITER.
	// Keeps the bot from getting hit with a burst of requests when a
	// playlist has many missing tracks at once.
	dispatchLimiter *rate.Limiter
}

func NewTelegram(cfg config.Telegram, spotifyCfg config.Spotify, downloadDir string, httpClient *util.HttpClient) *Telegram {
	interval := cfg.DispatchIntervalSeconds
	if interval <= 0 {
		interval = 20
	}
	return &Telegram{
		Cfg:             cfg,
		HttpClient:      util.NewHttp(util.HttpClientConfig{Timeout: cfg.Timeout}),
		DownloadDir:     downloadDir,
		Spotify:         newSpotifyResolver(spotifyCfg, httpClient),
		dispatchLimiter: rate.NewLimiter(rate.Every(time.Duration(interval)*time.Second), 1),
	}
}

func (c *Telegram) headers() map[string]string {
	return map[string]string{"X-API-Key": c.Cfg.AdminAPIKey}
}

// QueryTrack resolves the track to a Spotify link and stashes it on track.ID
// (same pattern slskd uses: ID is a scratch field for the search handle).
func (c *Telegram) QueryTrack(track *models.Track) error {
	link, err := c.Spotify.ResolveTrack(track)
	if err != nil {
		return fmt.Errorf("[telegram] spotify resolution failed for '%s - %s': %w", track.CleanTitle, track.MainArtist, err)
	}
	track.ID = link
	return nil
}

type telegramRequestPayload struct {
	SpotifyURL string `json:"spotify_url"`
	Title      string `json:"title"`
	Artist     string `json:"artist"`
	Album      string `json:"album"`
	TargetID   string `json:"target_user_id"`
}

type telegramRequestResp struct {
	JobID string `json:"job_id"`
}

// GetTrack submits the resolved Spotify link to your Telegram bot, which
// looks it up against your owned FLAC DB and drops the file into DownloadDir.
// track.MainArtistID is reused as the correlation/job ID (mirrors how slskd
// reuses MainArtistID to stash the uploading username).
func (c *Telegram) GetTrack(track *models.Track) error {
	// Wait for a free slot before hitting the bot - keeps requests spaced
	// out so it has time to look up + send each FLAC before the next one
	// lands, rather than getting a burst all at once.
	if err := c.dispatchLimiter.Wait(context.Background()); err != nil {
		return fmt.Errorf("[telegram] rate limiter wait failed: %w", err)
	}

	payload, err := json.Marshal(telegramRequestPayload{
		SpotifyURL: track.ID,
		Title:      track.CleanTitle,
		Artist:     track.MainArtist,
		Album:      track.Album,
		TargetID:   c.Cfg.TargetUserID,
	})
	if err != nil {
		return fmt.Errorf("failed to marshal telegram request: %w", err)
	}

	body, err := c.HttpClient.MakeRequest(
		"POST",
		c.Cfg.BotAPIURL+"/explo/request",
		bytes.NewReader(payload),
		c.headers(),
	)
	if err != nil {
		return fmt.Errorf("[telegram] failed to submit request for '%s': %w", track.CleanTitle, err)
	}

	var resp telegramRequestResp
	if err := util.ParseResp(body, &resp); err != nil {
		return err
	}

	track.MainArtistID = resp.JobID // job ID, polled by the Monitor loop below
	slog.Info("[telegram] queued track", "track", track.CleanTitle, "artist", track.MainArtist, "job_id", resp.JobID)
	return nil
}

// ── Monitor implementation ──────────────────────────────────────────────────

type telegramJobStatus struct {
	JobID    string `json:"job_id"`
	State    string `json:"state"`    // "queued" | "sent" | "done" | "not_found" | "error"
	Filename string `json:"filename"` // filename as dropped into DownloadDir, set once state=="done"
}

func (c *Telegram) GetConf() (MonitorConfig, error) {
	return MonitorConfig{
		CheckInterval:   time.Duration(c.Cfg.MonitorConfig.Interval) * time.Minute,
		MonitorDuration: time.Duration(c.Cfg.MonitorConfig.Duration) * time.Minute,
		MigrateDownload: false, // bot writes directly into DownloadDir, nothing to migrate
		Service:         "telegram",
	}, nil
}

func (c *Telegram) GetDownloadStatus(tracks []*models.Track) (map[string]FileStatus, error) {
	fileStatuses := make(map[string]FileStatus, len(tracks))

	for _, track := range tracks {
		// Only check tracks whose MainArtistID is a real Telegram job ID (32-char hex),
		// not a 36-char MusicBrainz artist MBID with dashes.
		if track.MainArtistID == "" || len(track.MainArtistID) != 32 || strings.Contains(track.MainArtistID, "-") {
			continue
		}
		reqURL := fmt.Sprintf("%s/explo/status/%s", c.Cfg.BotAPIURL, track.MainArtistID)
		body, err := c.HttpClient.MakeRequest("GET", reqURL, nil, c.headers())
		if err != nil {
			slog.Warn("[telegram] status check failed", "job_id", track.MainArtistID, "context", err.Error())
			continue
		}

		var status telegramJobStatus
		if err := util.ParseResp(body, &status); err != nil {
			continue
		}

		switch status.State {
		case "done":
			track.File = status.Filename
			fileStatuses[track.File] = FileStatus{
				ID:              status.JobID,
				Filename:        status.Filename,
				State:           "Succeeded",
				PercentComplete: 100,
			}
		case "not_found", "error":
			// stash a placeholder keyed by current track.File (empty until "done")
			// so the Monitor loop's failureStates path can pick it up and skip cleanly
			fileStatuses[track.File] = FileStatus{
				ID:    status.JobID,
				State: "Errored",
			}
		default: // "queued" / "sent" — still in progress, report nothing yet
			continue
		}
	}

	if len(fileStatuses) != 0 {
		return fileStatuses, nil
	}
	return nil, fmt.Errorf("no files found to monitor")
}

func (c *Telegram) Cleanup(track models.Track, jobID string) error {
	if jobID == "" {
		return nil
	}
	reqURL := fmt.Sprintf("%s/explo/job/%s", c.Cfg.BotAPIURL, jobID)
	if _, err := c.HttpClient.MakeRequest("DELETE", reqURL, nil, c.headers()); err != nil {
		return fmt.Errorf("failed to clean up telegram job: %w", err)
	}
	return nil
}