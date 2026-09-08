package broker

import (
	"bolt/internal/common"
	"context"
	"encoding/json"
	"log"
	"strings"
	"sync"

	"github.com/redis/go-redis/v9"
)

// Transmitter owns ConnStore plus one shared Redis PubSub connection,
// (un)subscribing stock channels on it as listeners come and go.
type Transmitter struct {
	mu        sync.Mutex
	connStore *ConnStore
	rdb       *redis.Client
	namespace string
	ps        *redis.PubSub
}

func NewTransmitter(rdb *redis.Client, namespace string) *Transmitter {
	ps := rdb.Subscribe(context.Background())

	t := &Transmitter{
		connStore: NewConnStore(),
		rdb:       rdb,
		namespace: namespace,
		ps:        ps,
	}
	go t.consume()
	return t
}

func (t *Transmitter) channel(stock string) string {
	return t.namespace + ":" + stock
}

func (t *Transmitter) AddConnection(stock string, client *common.Client) {
	t.mu.Lock()
	defer t.mu.Unlock()

	wasEmpty := len(t.connStore.GetClientsForStock(stock)) == 0
	t.connStore.AddConnection(stock, client)
	if wasEmpty {
		if err := t.ps.Subscribe(context.Background(), t.channel(stock)); err != nil {
			log.Println("redis subscribe error:", err)
		}
	}
}

func (t *Transmitter) RemoveStockForClient(client *common.Client, stock string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	t.connStore.RemoveStockForClient(client, stock)
	if len(t.connStore.GetClientsForStock(stock)) == 0 {
		t.unsubscribeStock(stock)
	}
}

// RemoveConnection drops client entirely, unsubscribing any stock channel
// left with no other listeners.
func (t *Transmitter) RemoveConnection(client *common.Client) {
	t.mu.Lock()
	defer t.mu.Unlock()

	stocks := t.connStore.RemoveClient(client)
	for _, stock := range stocks {
		if len(t.connStore.GetClientsForStock(stock)) == 0 {
			t.unsubscribeStock(stock)
		}
	}
}

// unsubscribeStock must be called with t.mu held.
func (t *Transmitter) unsubscribeStock(stock string) {
	if err := t.ps.Unsubscribe(context.Background(), t.channel(stock)); err != nil {
		log.Println("redis unsubscribe error:", err)
	}
}

func (t *Transmitter) consume() {
	for msg := range t.ps.Channel() {
		stock := strings.TrimPrefix(msg.Channel, t.namespace+":")

		var tick common.Tick
		if err := json.Unmarshal([]byte(msg.Payload), &tick); err != nil {
			log.Println("bad tick payload:", err)
			continue
		}
		t.sendTick(stock, tick)
	}
}

func (t *Transmitter) sendTick(stock string, tick common.Tick) {
	msg := common.ServerMessage{Type: common.TypeTick, Payload: tick}
	for _, client := range t.connStore.GetClientsForStock(stock) {
		client.Send(msg)
	}
}
