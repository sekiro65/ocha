package main

import (
	"fmt"

	"explo/src/config"
)

func main() {
	var cfg config.Config
	if err := cfg.GetFlags(); err != nil {
		fmt.Println("GetFlags error:", err)
		return
	}
	cfg.ReadEnv()

	fmt.Printf("SPOTIFY_CLIENT_ID:     %q (len=%d)\n", cfg.DownloadCfg.Spotify.ClientID, len(cfg.DownloadCfg.Spotify.ClientID))
	fmt.Printf("SPOTIFY_CLIENT_SECRET: %q (len=%d)\n", cfg.DownloadCfg.Spotify.ClientSecret, len(cfg.DownloadCfg.Spotify.ClientSecret))
	fmt.Printf("TELEGRAM_BOT_API_URL:  %q\n", cfg.DownloadCfg.Telegram.BotAPIURL)
	fmt.Printf("DOWNLOAD_SERVICES:     %v\n", cfg.DownloadCfg.Services)
}
