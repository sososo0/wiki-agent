# Distributed Coordination

## Distributed Locks

A distributed lock is a synchronization mechanism that ensures only one process across multiple machines can access a critical resource at any given time. Unlike single-machine locks (mutexes), distributed locks must handle network partitions, process crashes, and asynchronous message delivery.

Distributed locks are essential when multiple services need coordinated access to shared resources—database migration coordination, preventing duplicate job processing, or ensuring singleton operations. Implementation typically involves a coordination service like ZooKeeper, etcd, or Redis with appropriate TTL mechanisms.

Tradeoffs include added latency for lock acquisition, potential deadlocks if holders crash without releasing, and cascading failures if the coordination service becomes unavailable. Strong consistency guarantees (linearizable locks) are more robust but slower than eventual consistency models.

Example: A distributed cron job scheduler uses a lock in etcd to ensure only one instance executes a critical task. Services attempt lock acquisition before running the job; the lock holder releases it upon completion or timeout.

## Lock Leases and TTLs

Leases extend distributed locks by associating them with automatic expiration times (TTLs). A lease-holder must periodically renew the lease; if renewal fails (due to crash or network partition), the lock automatically releases after the TTL expires.

Leases solve the fundamental problem of crashed processes holding locks indefinitely. They're critical for safety-critical coordination where stale locks could cause data corruption or inconsistent state.

Challenges include choosing appropriate TTL values—too short causes false positives and thrashing, too long delays failure detection. Clock skew between machines can cause unexpected lease expirations or extensions.

Example: A distributed cache invalidation system uses 30-second leases. Holders heartbeat every 10 seconds; if a process crashes, the lease expires after 30 seconds, allowing another instance to acquire it and resume invalidation.

## ZooKeeper Ephemeral Nodes

ZooKeeper's ephemeral nodes automatically disappear when the client connection closes or times out. This provides a clean mechanism for tracking process liveness and implementing distributed locks.

Ephemeral nodes are particularly useful for leader election and maintaining metadata about active services. They eliminate cleanup logic—stale registrations automatically vanish. The connection timeout (session timeout) determines how quickly failures are detected.

The main tradeoff is that session timeout must balance responsiveness against false positives from transient network issues. Setting it too low causes rapid leader changes; too high delays failure detection. ZooKeeper maintains session state even across TCP connection failures if reconnection happens within the timeout window.

Example: Service discovery registers instances as ephemeral nodes under `/services/api-server/`. When a server crashes, its node automatically disappears, and clients reading the service list stop routing to it without explicit deregistration.

## etcd Leases

etcd's lease API associates key-value pairs with a time-to-live, requiring periodic renewal to maintain the association. This enables distributed locking, service registration, and session management with automatic cleanup.

Unlike ZooKeeper's implicit session management, etcd leases require explicit keepalive RPCs from clients. This model is more flexible but places responsibility on clients to implement renewal logic correctly.

Lease revocation (either by expiration or explicit cancellation) atomically removes all associated keys. This transactional guarantee is useful for atomic cleanup—releasing a lock also removes the corresponding metadata.

Example: A distributed configuration system uses etcd leases for feature flag locks. A service acquires a lease, then creates a key storing the flag value under that lease. If the service crashes, the lease expires and the feature flag automatically reverts to the previous value.

## Consensus and Agreement Protocols

Consensus protocols enable a distributed system to agree on a single value despite failures. Raft and Paxos are the primary algorithms used in modern coordination services, providing safety guarantees that leader election and state replication remain correct even when nodes fail or network partitions occur.

Consensus is necessary for building strongly consistent coordination services. Without it, split-brain scenarios can occur where multiple leaders make conflicting decisions. Raft's understandability has made it the dominant choice for new systems.

The cost is latency—reaching consensus requires communication between a quorum of nodes. Systems typically use consensus for infrequent critical decisions, not for every operation.

Example: etcd uses Raft consensus to ensure all replicas agree on the current key-value state and leader identity. When the leader fails, the remaining cluster reaches consensus on a new leader without requiring administrator intervention.

## Leader Election

Leader election is the process of distributed processes agreeing on a single leader to coordinate work or make decisions. The leader might schedule jobs, manage a resource, or serve as the primary write target.

Leader election is fundamental when you need a single coordinator. It prevents split-brain scenarios where multiple leaders make conflicting decisions. Coordination services like ZooKeeper and etcd provide primitives making leader election straightforward.

Tradeoffs include the latency of electing a new leader (typically 100ms to several seconds) and the complexity of handling leader transitions. Applications must gracefully handle periods without a leader or brief periods with multiple leaders during transitions.

Example: A database replication system uses etcd to elect the primary replica. The primary holds a key with a lease; if it crashes, the lease expires and another replica wins the election to become the new primary.

## Barrier Synchronization

A barrier is a coordination primitive that blocks processes until a specified number of participants have reached that point. This enforces ordering—no process proceeds until all have arrived at the barrier.

Barriers are useful for phased operations: waiting for all workers to complete a phase before starting the next, or ensuring all replicas reach a quorum before committing changes. They make distributed algorithms more predictable.

Barriers can cause deadlocks if a process crashes before reaching the barrier—other processes wait indefinitely. Timeout-based barriers mitigate this, though they introduce additional complexity.

Example: A distributed test framework uses ZooKeeper barriers to synchronize test phases. All test workers must reach a barrier before the framework proceeds to the next test phase, ensuring deterministic test execution.

## Watch Mechanisms

Watches in ZooKeeper and etcd allow clients to subscribe to notifications when specific keys or paths change. This enables reactive patterns where processes respond immediately to coordination events rather than polling.

Watches are more efficient than polling for detecting changes in small, frequently-accessed values. They reduce latency and server load when many clients need to react to updates.

Watch semantics vary between systems. ZooKeeper provides one-time triggers requiring re-registration; etcd provides continuous streaming watches. Both have edge cases around watch ordering and guarantees during failovers.

Example: A service discovery system sets watches on `/services/database/`. When a database instance becomes unavailable and its entry is deleted, all watching clients receive a notification and update their connection pools without waiting for the next poll interval.

## Quorum-Based Systems

Quorum-based coordination uses a majority (or other subset) of nodes for decisions, rather than requiring all nodes. A quorum of N nodes survives up to ⌊N/2⌋-1 failures and ensures that any two quorums overlap, preventing divergent decisions.

Quorum systems are more resilient than requiring unanimous agreement. They allow systems to remain available despite node failures. Most modern consensus protocols use quorum-based voting.

The cost is that quorum systems cannot operate during network partitions that split the cluster into groups smaller than the quorum size. The available partition stops making progress, guaranteeing safety at the cost of availability.

Example: A 5-node etcd cluster requires 3 nodes for quorum. It tolerates losing 2 nodes and remains operational. If a network partition creates two groups of 3 and 2 nodes, only the 3-node group continues operating.

## Split-Brain Scenarios

Split-brain occurs when a network partition causes a cluster to split into isolated subgroups, each believing it's the authoritative partition. Without prevention mechanisms, both partitions might elect leaders and make conflicting changes.

Split-brain is a fundamental hazard in distributed systems. It causes data inconsistency, duplicate operations, and corruption when both partitions continue processing requests. Prevention through quorum-based decisions is essential for safety.

Quorum systems prevent split-brain through partition tolerance—only the partition containing a quorum can proceed. Smaller partitions must stop and cannot make conflicting decisions. This sacrifices some availability to guarantee consistency.

Example: In a 3-node system split into {Node1} and {Node2, Node3}, the pair can elect a new leader and continue (they have quorum), while Node1 recognizes it has lost quorum and stops processing writes, preventing divergence.

## Write-Ahead Logging in Coordination

Coordination services use write-ahead logging (WAL) to durably record decisions before applying them. This ensures that committed decisions survive server restarts and remain consistent across replicas.

WAL is critical for durability. Without it, a server crash could lose committed coordination state, breaking safety guarantees. The coordination service can replay the log on restart to reconstruct state.

The cost is disk I/O latency for each write. Systems optimize through batching, fsync tuning, and SSD hardware to minimize latency impact.

Example: etcd writes every key-value modification to its WAL on disk before applying it to the in-memory state machine. On restart, it replays the log to recover the exact pre-crash state.

## Heartbeat Mechanisms

Heartbeats are periodic messages sent from clients to servers (or servers to clients) to signal liveness. They're the foundation of failure detection—when heartbeats stop, the receiver assumes failure.

Heartbeats are nearly universal in distributed systems. They provide explicit, configurable failure detection rather than waiting for an operation to fail. The heartbeat interval/timeout trade-off directly controls how quickly failures are detected.

Aggressive heartbeating (short intervals) improves failure detection speed but increases network load and false-positive rates. Conservative heartbeating reduces load but increases failure detection latency.

Example: A distributed lock holder sends heartbeats to the coordination service every 5 seconds to renew its lease. If heartbeats stop for 15 seconds, the server assumes the holder crashed and releases the lock.

## Partition Tolerance and CAP Trade-offs

The CAP theorem states systems can guarantee at most two of: Consistency, Availability, and Partition tolerance. Modern coordination services prioritize consistency and partition tolerance, sacrificing availability during partitions.

Partition tolerance is mandatory—network partitions happen in practice. Most systems choose CP (consistency and partition tolerance), meaning they stop the minority partition rather than serving stale data.

Understanding CAP helps reason about coordination system behavior. During a partition, the available partition continues operating at the cost of potential unavailability in other partitions. This is usually correct for coordination but unacceptable for application data.

Example: A distributed lock service stops accepting requests in partitions without quorum to guarantee only one lock holder exists globally, even if it temporarily cannot serve some clients.

## Clock Skew and Synchronization

Distributed systems often use time for TTL expiration, detecting timeouts, and ordering events. Clock skew (differences between machines' clocks) can cause leases to expire unexpectedly or ordering violations.

Clock skew is a practical problem—typical clock drift is 100-500ppm, meaning a clock can skew by ~1 second per hour. Systems cannot assume clocks are synchronized and must account for skew in timeout calculations.

Solutions include NTP (for rough synchronization with ~100ms accuracy), atomic clocks (expensive, limited to critical infrastructure), or skew-aware algorithms that add buffers to timeouts or use logical clocks instead of physical time.

Example: A distributed lock with a 10-second TTL might be revoked after only 8 wall-clock seconds if the server's clock runs fast and the client's clock runs slow. Algorithms add buffers (e.g., 1-second safety margin) to handle this.

## Linearizable Consistency

Linearizability provides the strongest consistency guarantee: all operations appear to take effect instantaneously at some point between their invocation and response. The execution order respects real-time ordering.

Linearizability is necessary for coordination primitives like locks—if lock release and acquisition are not linearizable, races can occur. It prevents stale reads that might violate invariants.

Achieving linearizability requires quorum-based reads/writes or leader-based reads (after verifying leadership). Both add latency compared to weak consistency. High-performance systems sometimes accept weaker guarantees for specific operations.

Example: An etcd read with linearizable consistency guarantees it reflects all writes completed before the read started. This ensures a client reading a lock after receiving an unlock confirmation sees the lock released.

## Witness Nodes

Witness nodes (or arbiters) participate in quorum decisions without storing full data. They're cheaper than regular nodes—no disk storage, minimal memory—but can provide quorum membership for availability.

Witnesses are useful for large deployments where adding data replicas is expensive but increasing availability margins is valuable. They reduce the quorum size relative to total nodes, improving fault tolerance.

The tradeoff is that witnesses cannot serve read operations—they store no data. They're only useful for voting in consensus. Systems must carefully design which nodes participate in quorum to maintain data availability.

Example: A 5-node cluster might use 3 data replicas and 2 witness nodes. Quorum requires 3 nodes; the witnesses participate in quorum decisions but cannot respond to reads, freeing resources for other purposes.

## Transaction Coordination

Distributed transactions coordinate multiple services to make consistent updates across databases. Coordination services often manage transaction commit protocols, two-phase commit orchestration, or saga state management.

Transaction coordination is essential for maintaining consistency across multiple databases. It prevents partial updates where one database commits but another fails. The complexity increases with geographic distribution and network latency.

Distributed transactions are slow and difficult to reason about. Modern systems often prefer eventual consistency with compensating transactions (sagas) over strong ACID guarantees across services.

Example: An order processing system uses a coordination service to track saga state across payment, inventory, and shipping services. The coordinator records each step and enforces rollback logic if any service fails.

## Recovery and Resynchronization

When a failed node restarts, it must recover persisted state and resynchronize with the cluster to rejoin the consensus group. Recovery mechanisms determine how quickly nodes become useful after failure.

Fast recovery is important for availability—a node stuck recovering cannot serve requests or contribute to quorum. Efficient recovery mechanisms minimize the window where the cluster operates below full strength.

Approaches include snapshot-based recovery (faster but requires periodic snapshots) and log replay (simpler but slower). Hybrid approaches use snapshots with incremental log replay for balance.

Example: An etcd member crashes and restarts. It loads the latest snapshot and replays the WAL log to recover exact state. The leader sends recent log entries to fully synchronize it before it becomes available for reads.

## Fencing and Token-Based Systems

Fencing tokens prevent split-brain scenarios by requiring clients to prove they still hold a valid token before modifying protected resources. The token is revoked when lock ownership changes.

Fencing adds a second layer of safety. Even if a client incorrectly believes it still holds a lock (due to delayed notification), the resource verifies the token before allowing modification. This catches logic errors in the coordination protocol.

Fencing tokens require resource servers to check them—the coordination service alone cannot enforce them. All code accessing protected resources must implement token verification.

Example: A distributed database uses fencing with locks. When a client acquires a lock for exclusive access, it receives a token. The database rejects writes with outdated tokens, even if the client hasn't yet received revocation notification.

## Bulk Operations and Atomic Transactions

Some coordination services support multi-operation transactions that atomically succeed or fail as a unit. This enables complex coordination logic within the service rather than through client-side retry logic.

Atomic transactions in the coordination service simplify complex workflows and prevent partial state updates. They're more reliable than client-side coordination because the server ensures atomicity regardless of client failures.

Transactional capability varies widely—some services support only compare-and-swap, others support full ACID transactions. More powerful transactions require more sophisticated consensus and recovery mechanisms.

Example: etcd transactions let a client atomically check a condition and perform multiple updates if it's true. A service can check if a lock is available and atomically acquire it in a single operation, preventing race conditions.
