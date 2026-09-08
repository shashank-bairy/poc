package broker

import (
	"bolt/internal/common"
	"sync"
)

type ConnStore struct {
	mu              sync.RWMutex
	stocksToClients map[string][]*common.Client
	clientToStocks  map[*common.Client][]string
}

func NewConnStore() *ConnStore {
	return &ConnStore{
		stocksToClients: make(map[string][]*common.Client),
		clientToStocks:  make(map[*common.Client][]string),
	}
}

func (cs *ConnStore) AddConnection(stock string, client *common.Client) {
	cs.mu.Lock()
	defer cs.mu.Unlock()

	cs.stocksToClients[stock] = append(cs.stocksToClients[stock], client)
	cs.clientToStocks[client] = append(cs.clientToStocks[client], stock)
}

func (cs *ConnStore) GetClientsForStock(stock string) []*common.Client {
	cs.mu.RLock()
	defer cs.mu.RUnlock()

	return cs.stocksToClients[stock]
}

// swapRemove drops v from slice (order not preserved), O(1).
func swapRemove[T comparable](slice []T, v T) []T {
	for i, x := range slice {
		if x == v {
			last := len(slice) - 1
			slice[i] = slice[last]
			return slice[:last]
		}
	}
	return slice
}

func (cs *ConnStore) removeClientFromStock(stock string, client *common.Client) {
	if clients := swapRemove(cs.stocksToClients[stock], client); len(clients) == 0 {
		delete(cs.stocksToClients, stock)
	} else {
		cs.stocksToClients[stock] = clients
	}
}

func (cs *ConnStore) removeStockFromClient(client *common.Client, stock string) {
	if stocks := swapRemove(cs.clientToStocks[client], stock); len(stocks) == 0 {
		delete(cs.clientToStocks, client)
	} else {
		cs.clientToStocks[client] = stocks
	}
}

func (cs *ConnStore) RemoveStockForClient(client *common.Client, stock string) {
	cs.mu.Lock()
	defer cs.mu.Unlock()

	cs.removeClientFromStock(stock, client)
	cs.removeStockFromClient(client, stock)
}

// RemoveClient drops client from every stock it was subscribed to and
// returns the list of affected stocks, so callers can react (e.g. drop
// upstream subscriptions that now have no listeners left).
func (cs *ConnStore) RemoveClient(client *common.Client) []string {
	cs.mu.Lock()
	defer cs.mu.Unlock()

	stocks := cs.clientToStocks[client]
	for _, stock := range stocks {
		cs.removeClientFromStock(stock, client)
	}
	delete(cs.clientToStocks, client)
	return stocks
}
