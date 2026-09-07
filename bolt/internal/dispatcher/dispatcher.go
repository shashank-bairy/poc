package dispatcher

import (
	"bolt/internal/common"
	"encoding/json"
	"fmt"
	"log"
	"net/http"

	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
)

const maxMessageSize = 4096

type Dispatcher struct {
	rdb         *redis.Client
	namespace   string
	transmitter *Transmitter
}

func NewDispatcher(redisAddr, namespace string) *Dispatcher {
	rdb := redis.NewClient(&redis.Options{
		Addr: redisAddr,
	})
	return &Dispatcher{rdb: rdb, namespace: namespace, transmitter: NewTransmitter(rdb, namespace)}
}

func (d *Dispatcher) wsHandler(w http.ResponseWriter, r *http.Request) {
	var upgrader = websocket.Upgrader{
		ReadBufferSize:  1024,
		WriteBufferSize: 1024,
		CheckOrigin: func(r *http.Request) bool {
			// Allow all origins for now
			return true
		},
	}

	// Upgrade the HTTP connection to a WebSocket connection
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Println("upgrade error:", err)
		return
	}
	defer conn.Close()
	conn.SetReadLimit(maxMessageSize)

	client := common.NewClient(conn)
	defer d.transmitter.RemoveConnection(client)
	defer client.Close()

	go d.writeLoop(conn, client)

	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			log.Println("read error:", err)
			break
		}

		d.processClientMessage(client, msg)
	}
}

func (d *Dispatcher) writeLoop(conn *websocket.Conn, client *common.Client) {
	for msg := range client.Outbox() {
		if err := conn.WriteJSON(msg); err != nil {
			log.Println("write error:", err)
			return
		}
	}
}

func (d *Dispatcher) sendError(client *common.Client, message string) {
	client.Send(common.ServerMessage{
		Type:    common.TypeError,
		Payload: common.ErrorPayload{Message: message},
	})
}

func (d *Dispatcher) processClientMessage(client *common.Client, raw []byte) {
	var msg common.ClientMessage
	if err := json.Unmarshal(raw, &msg); err != nil {
		d.sendError(client, "invalid message: "+err.Error())
		return
	}

	switch msg.Type {
	case common.TypeSubscribe:
		var req common.SubRequest
		if err := json.Unmarshal(msg.Payload, &req); err != nil {
			d.sendError(client, "invalid subscribe payload: "+err.Error())
			return
		}
		if err := req.Validate(); err != nil {
			d.sendError(client, err.Error())
			return
		}
		d.transmitter.AddConnection(req.StockId, client)

	case common.TypeUnsubscribe:
		var req common.UnSubRequest
		if err := json.Unmarshal(msg.Payload, &req); err != nil {
			d.sendError(client, "invalid unsubscribe payload: "+err.Error())
			return
		}
		if err := req.Validate(); err != nil {
			d.sendError(client, err.Error())
			return
		}
		d.transmitter.RemoveStockForClient(client, req.StockId)

	default:
		d.sendError(client, fmt.Sprintf("unknown message type: %s", msg.Type))
	}
}

func (d *Dispatcher) Start() {
	http.HandleFunc("/ws", d.wsHandler)

	log.Println("listening on :8000")
	if err := http.ListenAndServe(":8000", nil); err != nil {
		log.Fatal(err)
	}

}
