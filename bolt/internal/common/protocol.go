package common

type MessageType string

const (
	TypeSubscribe   MessageType = "subscribe"
	TypeUnsubscribe MessageType = "unsubscribe"
	TypeAck         MessageType = "ack"
	TypeError       MessageType = "error"
	TypeTick        MessageType = "tick"
)
