package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

type Tick struct {
	StockId string `json:"stockId"`
	Price   int64  `json:"price"`
	Ltt     int64  `json:"ltt"`
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	brokerAddr := getEnv("BROKER_ADDR", "localhost:9000")

	var stocks []string
	if n, err := strconv.Atoi(getEnv("STOCK_COUNT", "")); err == nil && n > 0 {
		for i := 1; i <= n; i++ {
			stocks = append(stocks, fmt.Sprintf("STOCK%d", i))
		}
	} else {
		stocks = strings.Split(getEnv("STOCKS", "AAPL,GOOG"), ",")
	}

	conn, err := net.Dial("tcp", brokerAddr)
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close()

	enc := json.NewEncoder(conn)
	price := int64(100)
	tickCount := 0
	for {
		for _, s := range stocks {
			price++
			t := Tick{StockId: s, Price: price, Ltt: time.Now().Unix()}
			if err := enc.Encode(t); err != nil {
				log.Fatal(err)
			}
			tickCount++
		}
		log.Printf("sent %d ticks across %d stocks (round done)", tickCount, len(stocks))
		time.Sleep(1 * time.Second)
	}
}
