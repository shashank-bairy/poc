package main

import (
	"bolt/internal/broker"
	"bolt/internal/dispatcher"
	"os"
)

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	redisAddr := getEnv("REDIS_ADDR", "localhost:6379")
	namespace := getEnv("NAMESPACE", "bolt")

	d := dispatcher.NewDispatcher(redisAddr, namespace)
	go d.Start()

	b := broker.NewBroker(redisAddr, namespace)
	b.Start()
}
