package broker

import (
	"bolt/internal/common"
	"context"
	"encoding/json"
	"io"
	"log"
	"net"
	"time"

	"github.com/redis/go-redis/v9"
)

type Broker struct {
	rdb       *redis.Client
	namespace string
}

func NewBroker(redisAddr, namespace string) *Broker {
	rdb := redis.NewClient(&redis.Options{
		Addr: redisAddr,
	})
	return &Broker{rdb: rdb, namespace: namespace}
}

func (b *Broker) Start() {
	ln, err := net.Listen("tcp", ":9000")
	if err != nil {
		log.Fatal(err)
	}
	defer ln.Close()

	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Println(err)
			continue
		}
		go b.handleConn(conn)
	}
}

func (b *Broker) getChannel(id string) string {
	return (b.namespace + ":" + id)
}

func (b *Broker) handleConn(conn net.Conn) {
	defer conn.Close()
	decoder := json.NewDecoder(conn)
	ctx := context.Background()

	for {
		var tick common.Tick
		if err := decoder.Decode(&tick); err != nil {
			if err != io.EOF {
				log.Println("decode error:", err)
			}
			return
		}
		log.Printf("tick: %+v\n", tick)

		payload, err := json.Marshal(tick)
		if err != nil {
			log.Println("marshal error:", err)
			continue
		}

		channel := b.getChannel(tick.StockId)

		publishCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		if err := b.rdb.Publish(publishCtx, channel, payload).Err(); err != nil {
			log.Println("redis publish error:", err)
		}
		cancel()
	}
}
