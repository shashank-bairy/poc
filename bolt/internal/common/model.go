package common

import (
	"encoding/json"
	"errors"
	"sync"

	"github.com/gorilla/websocket"
)

// Market data

type Tick struct {
	StockId string `json:"stockId"`
	Price   int64  `json:"price"`
	Ltt     int64  `json:"ltt"`
}

// Connection

type Client struct {
	conn   *websocket.Conn
	send   chan ServerMessage
	userId string

	mu     sync.Mutex
	closed bool
}

func NewClient(conn *websocket.Conn) *Client {
	return &Client{
		conn: conn,
		send: make(chan ServerMessage, 16),
	}
}

// Send queues msg for delivery to the client. Non-blocking: if the
// client's outbox is full (slow/stuck reader), the message is dropped
// rather than stalling the broadcaster. No-op once the client is closed.
func (c *Client) Send(msg ServerMessage) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return
	}

	select {
	case c.send <- msg:
	default:
	}
}

// Close closes the outbox so the writer goroutine draining Outbox()
// can exit. Safe to call once; safe to call concurrently with Send.
func (c *Client) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return
	}
	c.closed = true
	close(c.send)
}

// Outbox returns the channel a per-connection writer goroutine should
// drain and write to the socket.
func (c *Client) Outbox() <-chan ServerMessage {
	return c.send
}

// Wire envelopes (outer type/payload wrapper for all WS messages)

type ClientMessage struct {
	Type    MessageType     `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

type ServerMessage struct {
	Type    MessageType `json:"type"`
	Payload interface{} `json:"payload"`
}

// Client -> server payloads

type SubRequest struct {
	UserId  string `json:"userId"`
	StockId string `json:"stockId"`
}

type UnSubRequest struct {
	UserId  string `json:"userId"`
	StockId string `json:"stockId"`
}

func (r SubRequest) Validate() error {
	if r.UserId == "" {
		return errors.New("userId is required")
	}
	if r.StockId == "" {
		return errors.New("stockId is required")
	}
	return nil
}

func (r UnSubRequest) Validate() error {
	if r.UserId == "" {
		return errors.New("userId is required")
	}
	if r.StockId == "" {
		return errors.New("stockId is required")
	}
	return nil
}

// Server -> client payloads

type AckPayload struct {
	Action  MessageType `json:"action"`
	StockId string      `json:"stockId"`
}

type ErrorPayload struct {
	Message string `json:"message"`
}
