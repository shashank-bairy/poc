package dispatcher

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

type Dispatcher struct {
	rdb       *redis.Client
	namespace string
}

func NewDispatcher(redisAddr, namespace string) *Dispatcher {
	rdb := redis.NewClient(&redis.Options{
		Addr: redisAddr,
	})
	return &Dispatcher{rdb: rdb, namespace: namespace}
}

func (d *Dispatcher) Start() {
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
		go d.handleConn(conn)
	}
}

func (d *Dispatcher) getChannel(id string) string {
	return (d.namespace + ":" + id)
}

func (d *Dispatcher) handleConn(conn net.Conn) {
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

		channel := d.getChannel(tick.StockId)

		publishCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		if err := d.rdb.Publish(publishCtx, channel, payload).Err(); err != nil {
			log.Println("redis publish error:", err)
		}
		cancel()
	}
}
