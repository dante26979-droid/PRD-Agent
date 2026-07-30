package main

import (
	"context"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"
)

func TestMaintenanceControlLoopRunsWhileDispatchIsBlocked(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	dispatchStarted := make(chan struct{})
	var controlCalls atomic.Int32
	jobs := map[string]maintenanceJob{
		"dispatch": func(ctx context.Context) error {
			select {
			case <-dispatchStarted:
			default:
				close(dispatchStarted)
			}
			<-ctx.Done()
			return ctx.Err()
		},
		"control": func(context.Context) error {
			controlCalls.Add(1)
			return nil
		},
	}
	done := make(chan struct{})
	go func() {
		runMaintenanceJobs(ctx, 5*time.Millisecond, jobs, slog.New(slog.NewTextHandler(io.Discard, nil)))
		close(done)
	}()

	select {
	case <-dispatchStarted:
	case <-time.After(time.Second):
		t.Fatal("dispatch job did not start")
	}
	deadline := time.Now().Add(time.Second)
	for controlCalls.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if controlCalls.Load() < 2 {
		t.Fatal("control loop was starved by the blocked dispatch")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("maintenance jobs did not stop with their context")
	}
}

func TestMaintenanceJobRecoversAndContinuesAfterPanic(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var calls atomic.Int32
	done := make(chan struct{})
	go func() {
		runMaintenanceJobs(ctx, 5*time.Millisecond, map[string]maintenanceJob{
			"recoverable": func(context.Context) error {
				if calls.Add(1) == 1 {
					panic("transient adapter panic")
				}
				cancel()
				return nil
			},
		}, slog.New(slog.NewTextHandler(io.Discard, nil)))
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("maintenance job did not recover from panic")
	}
	if calls.Load() < 2 {
		t.Fatalf("maintenance job did not continue after panic: %d calls", calls.Load())
	}
}
