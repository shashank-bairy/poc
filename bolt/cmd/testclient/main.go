package main

import (
	"encoding/json"
	"log"
	"os"
	"strconv"
	"strings"

	"github.com/gorilla/websocket"
)

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	wsURL := getEnv("WS_URL", "ws://localhost:8000/ws")
	userId := getEnv("USER_ID", "u1")

	var stockIds []string
	if n, err := strconv.Atoi(getEnv("STOCK_COUNT", "")); err == nil && n > 0 {
		offset, _ := strconv.Atoi(getEnv("STOCK_OFFSET", "0"))
		for i := 1; i <= n; i++ {
			stockIds = append(stockIds, "STOCK"+strconv.Itoa(offset+i))
		}
	} else {
		stockIds = strings.Split(getEnv("STOCK_IDS", "AAPL"), ",")
	}

	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		log.Fatal("dial:", err)
	}
	defer conn.Close()

	for _, stockId := range stockIds {
		sub := map[string]interface{}{
			"type":    "subscribe",
			"payload": map[string]string{"userId": userId, "stockId": stockId},
		}
		b, _ := json.Marshal(sub)
		if err := conn.WriteMessage(websocket.TextMessage, b); err != nil {
			log.Fatal("write:", err)
		}
	}
	log.Printf("subscribed to %d stocks: %v", len(stockIds), stockIds)

	total := 0
	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			log.Fatal("read:", err)
		}
		total++
		log.Printf("recv (%d): %s", total, string(msg))
	}
}
