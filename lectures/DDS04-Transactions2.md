Web-scale Data Management
Transaction Processing & Concurrency
Control #2

Asterios Katsifodimos
George Christodoulou
Christoph Lofi

1

Today

1. Timestamp–based Concurrency Control
2. Database logging, and why it’s useful
3. Time & Relativity
4. How 1, 2, and 3 can get you fast geo-distributed

transactions

–

Example systems: Google Spanner & Amazon DSQL

2

Timestamp-based
Concurrency
Control

An optimistic approach

3

Reminder: two-phase locking

• Two-phase Locking

– Locks are granted in a growing phase
– Locks are released in a shrinking phase

• This means, that for each transaction all necessary
locks are acquired before the first lock is released.

• Result: serializability

#locks

lock point

lock
phase

unlock
phase

start TA

commit point

• BUT: what is the important assumption that 2PL makes?

– we expect lots of conflicts

4

Pessimists vs. Optimists & Concurrency Control

The world is a place of endless strife;
we must lock every data item, for
conflict is inevitable and suffering is
certain.

Nonsense! We live in the best of
all possible workloads, where
conflicts are rare and we need
only validate our harmony at the
end!

Arthur Schopenhauer
Famous Pessimist

Gottfried Wilhelm Leibniz
Famous Optimist

5

Locking vs. Optimism

• Pessimistic (Locking): Assumes conflicts are frequent.
Transactions must acquire locks before accessing data,
leading to waiting and potential deadlocks.

• Optimistic (Timestamps): Assumes conflicts are rare.
Transactions execute without waiting. Validation is
performed via timestamps to ensure serializability.
– No-locking = No deadlocks.
– Serialization Order: Determined by transaction start time.

6

Timestamp-ordering | Intuition

• Each transaction 𝑇! is assigned a unique timestamp TS 𝑇!  when it

reaches the system.

• 𝑇! is "older" than 𝑇",	if TS 𝑇! < TS 𝑇"

– If operations conflict, the order of execution must be equivalent to a serial

schedule where  𝑇!  precedes  𝑇" .

– Remember the serializability graph? No cycles here…

𝑇!

𝑇#

•

Intuition
– try to read a value. If another, younger transaction wrote on it, it’s dirty. You should

abort…

– Try to write on a value. If another, younger transaction has read/written on it, abort.

7

More formally in Timestamp-based CC

• Each read or write on a value gets a timestamp

• 𝑅𝑇𝑆 𝑇' : Read timestamp of 𝑇'
• 𝑊𝑇𝑆 𝑇'  Write timestamp of 𝑇'

• All data items are marked with this metadata

– i.e., when an item (row, page, etc.) is read or written, the

transaction manager takes note

8

Timestamp Order: the read rule

• Scenario: Transaction 𝑇' wants to R(X)

• Check for conflict:

– If 𝑇𝑆(𝑇!) 	 < 	𝑊𝑇𝑆(𝑋)	it means that a "younger" (later)
transaction has already written a new value to X.
•

If 𝑇! reads now, it would be reading a value from the "future"
relative to its start time.
• Action: Reject & Abort 𝑇!

– Else: Allow the read (and update metadata)

9

Timestamp Order: the write rule

• Scenario: Transaction 𝑇! wants to Write(X)

• Check for Read Conflict

– If 𝑇𝑆 𝑇! < 𝑅𝑇𝑆 𝑋  means that a "younger" transaction has already read the current

value of X. If 𝑇! overwrites it now, that younger transaction's read becomes invalid (it
should have read the value 𝑇! is about to write).

– Reject or Abort 𝑇!

• Check for Write Conflict

– If 𝑇𝑆 𝑇! < 𝑅𝑇𝑆 𝑋  A "younger" transaction has already written a newer value to X.

Ideally, 𝑇!	's write should have happened before that.

– Reject or Abort 𝑇!

•

If none of the above, allow the write (and update the write timestamp of X
too)!

10

Timestamp-based vs. Locking

Locking (2PL)

Timestamp Ordering

Conflict Resolution

Wait (Block)

Abort (Restart)

Deadlocks

Possible (Requires
detection)

Starvation

Possible

Impossible

High risk for long
transactions

Throughput

Better in high contention

Better in low contention

11

But how do we obtain timestamps?

• Timestamp Generation Methods
– System Clock (a.k.a. wall clock time)
– Logical Counter (sequence)

• What about distributed databases?

– Can we have properly ordered, and unique timestamps

across machines / regions?

12

Multiple Approaches

• Centralized transaction manager
– What most distributed RDBMS do

• Distributed consensus on timestamp/order
– Paxos is expensive; still used by some systems

13

Time & Relativity

14

Three locations, three different times

time=13:00:01

time=?

What time is it in
my database in
Germany?

time=13:00:02

time=13:00:00

15

There is no “now” in distributed systems

• Two remote machines cannot

perfectly synchronize their clocks
– using NTP for clock synchronization is
likely to give somewhere between
100ms and 250ms discrepancy

• Even if they sync, their “crystals”

beat in a slightly different
frequency and they drift apart
eventually.

16

Corrolary

• Two timestamps from different machines cannot

help us know whether a transaction on a machine
”happened before” another.
– In simplistic physics: this observation of who was first,

depends on the observer

Trick #1: Use Vector Clocks

Trick #2: Use sync error ε

Too much message exchange.

Embrace uncertainty

17

Google’s TrueTime

• Leverages hardware features like GPS, satelites,

and Atomic Clocks

• TrueTime API.

Method
TT.now()

TT.after(t)

TT.before(t)

Returns

TTinterval: [earliest, latest]

True if t  has passed

True if t has not arrived

18

Three locations, three different times

time=13:00:01

What time is it in
my database in
Germany?

time=13:00:02

Time ~=12:59:59 – 13:00:03

time=13:00:00

19

TrueTime

• “Global wall-clock time” with bounded uncertainty

• Consider event enow which invoked tt = TT.new()
• Guarantee: tt.earliest <= tt.now() <= tt.latest

20

Example 1: do we know who came first?

uncertainty

earliest

latest

T1

Commit?

T2

Commit?

Time

21

Example 2: do we know who came first?

uncertainty

earliest

latest

T1

T2

Commit?

Commit?

Time

22

Example 2: do we know who came first?

uncertainty

earliest

latest

If the intervals of two timestamps overlap, their absolute order cannot be immediately
determined with physical certainty, which is a key concept of TrueTime.

T2

T1

Time

23

Example 2: do we know who came first?

uncertainty

earliest

latest

BUT: if T2 “waits” for the uncertainty to pass, all is good*!

*this is more complex than presented here

T2

T1

T2

Time

24

TrueTime / Amazon Time Service) to order timestamps?

•

•

In Amazon DSQL: When a transaction wants
to commit, it must verify that no other
transaction has written to the same keys
between its start time (Ts) and commit time
(Tc).

In Google Spanner: similar  but with
pessimistic concurrency control and Two-
phase Locking.

• CockroachDB, hybrid approach with clock
corrections (and higher error margins).

25

Logging & Recovery

26

Operations – Example

Action

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

OUTPUT(A)

OUTPUT(B)

t

8

16

16

8

16

16

16

16

Mem A

Mem B

Disc A

Disc B

8

8

16

16

16

16

16

16

8

8

8

8

8

8

16

16

8

8

8

8

8

8

8

16

8

8

16

16

16

• Where may a system failure occur?
• Where must a system failure not occur?

27

Log Records

•

Several log record/event types:
– <START T> - Transaction T has begun
– <COMMIT T> - Transaction T has completed and does not change anymore

• General: Changes are not yet on disc necessarily (depends on buffer management strategy)
• But undo logging requires this

– <ABORT T> - Transaction T has aborted

•

The transaction manager must guarantee that T has no effect on the disk

– <T, X, v> - Update: Transaction T has changed element X
                              v is the old (and overwritten) value of X
•
• Will be written after WRITE
• Must be on the disk before the OUTPUT

Especially for undo logging: The new value is not necessary

28

Log Records

• Log file: Append-only

– logs of every single operation in a database

• Log-Manager saves each important event

• Log file also organized into blocks

– First in the main memory
– Then written to the disk(s)
– This makes logging more complex!

29

Undo Logging - Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

8

16

16

8

16

16

16

16

8

8

16

16

16

16

16

16

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

OUTPUT(B)

FLUSH LOG

8

8

8

8

8

8

16

16

8

8

8

8

8

8

8

16

8

8

16

16

16

<START T>

<T, A, 8>

<T, B, 8>

<COMMIT T>

30

Undo Logging – Theseus & Ariadne's thread!

“Ariadne gave Theseus a ball of red thread, and Theseus unrolled it as he penetrated the labyrinth, which
allowed him to find his way back out. He found the minotaur deep in the recesses of the labyrinth, killed it with
his sword, and followed the thread back to the entrance.”

31

“Formal” Rules of Undo Logging

• U1: If transaction T changes element X, <T, X, v> must have been
written in the log on the disk BEFORE writing the new value of X
on the disk

• U2: If transaction T commits, <COMMIT T> can only be written
into the log after all changed elements have been written to the
disk

• Writing to the disk is carried out in the following order:

1. Write log record for changed elements
2. Write elements to the disk
3. Write COMMIT log record
– 1. and 2. separately for each element!

32

Log Manager

• Log manager: FLUSH LOG command instructs the
buffer manager to write all log blocks to the disk
• Transaction manager: OUTPUT command instructs
the buffer manager to write element on the disk

– That implies: Every data block in the buffer manager has a
position of the log (log entry number) associated with it.

– Log must be written to that point before the block is

written on the disk.

33

Undo Logging - Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

OUTPUT(B)

FLUSH LOG

8

16

16

8

16

16

16

16

8

8

16

16

16

16

16

16

8

8

8

8

8

8

16

16

8

8

8

8

8

8

8

16

8

8

16

16

16

<START T>

<T, A, 8>

<T, B, 8>

<COMMIT T>

Necessary?

Transaction can be reported as committed, only
after flushing log. Required for correct recovery.

34

Recovery via Undo Logging

• Problem: System fault in the middle of transactions

– Atomicity not fulfilled
– Database inconsistent
– à Recovery manager to the rescue

• Naive approach – Examine the complete log
• Group all transactions

– “Committed“ if <COMMIT T> exists
– “Uncommitted“ otherwise (<START T> without <COMMIT T>)

•
•

Undo necessary!
Apply update records in log for undo

• Write the old value v for element X with respect to the update records

– Regardless of the actual value that is currently stored

35

Recovery via Undo Logging

• Problem

– Uncommitted transactions made changes to the database

• Solution: Undo recovery

– Process the complete log file backwards from end to start

“Chronologically backwards“

– Most recent values first

• When walking back

•

•
•

– Memorize all transactions with COMMIT or ABORT
– When update record <T, X, v>

If a COMMIT or ABORT exists for T: Do nothing
Otherwise: Write v on X

• At the end

– Write <ABORT X> for all uncommitted transactions into log
– FLUSH LOG

36

Recovery via Undo Logging -  Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

8

16

16

8

8

16

8

8

8

8

16
At Recovery:
16
8
• T (definitely) will be recognized as uncommitted

16

8

8

8

16

16

• Value 8 will be written to B
• Value 8 will be written to A
• <ABORT T> will be written to log
• Log will be flushed
16

16

16

8

16

16

8

8

8

8

8

8

8

OUTPUT(B)

16

16

16

16

16

FLUSH LOG

<START T>

<T, A, 8>

<T, B, 8>

Crash
<COMMIT T>

37

System Fault during Recovery

At system fault: Simply rerun log from the beginning
• Recovery steps are idempotent

– Repeated execution is equivalent to a single execution

• What if we have a huge log? Checkpoint!
– Replay: run the log to a certain checkpoint
– Store: save the materialized tables on disk
– Trim: delete all the “replayed” events from the log

38

Redo Logging - Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

OUTPUT(B)

8

16

16

8

16

16

16

16

8

8

16

16

16

16

16

16

8

8

8

8

8

8

16

16

8

8

8

8

8

8

8

16

8

8

16

16

16

<START T>

<T, A, 16>

<T, B, 16>

<COMMIT T>

39

“Formal” Redo Logging Rules

• Log record <T, X, v>

– For database element X, transaction T has written new value v

• Redo Rule (“write-ahead logging“ rule)

– R1: Before any database element X changed by T is written to the
disk, all log records of T and COMMIT T must be written to log

• Writing to the disk is carried out in the following order:

– Write update log records on the disk
– Write COMMIT log record on the disk
– Write changed the database elements on the disk

40

Redo Logging - Recovery

• Observation:

– If no COMMIT in log, elements on the disk are untouched

•

There is no need to recover them
– => Incomplete transactions can be ignored
• Committed transactions are a problem

•

– It is not clear, which changes are on the disk right now
– But: Log records hold all information about that
Process:
– Identify committed transactions
– Read log data from the beginning to the end (chronological)

•
•
•

For each update record <T, X, v>
If T is not committed: Ignore
If T is committed: Write v as element X

– For each uncommitted transaction write <ABORT T> into log
– Flush log

41

Redo Logging – Recovery Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

OUTPUT(B)

8

16

16

8

16

16

16

16

8

8

16

16

16

16

16

16

8

8

8

8

8

At Recovery:
• T will be recognized as committed

8

8

16

• Value 16 will be written for A
   (possibly redundant)
• Value 16 will be written for B
   (possibly redundant)

8

16

16

16

16

<START T>

<T, A, 16>

<T, B, 16>

<COMMIT T>

8

8

8

8

8

8

8

16

Crash

42

Redo-Logging – Recovery Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

OUTPUT(B)

8

16

16

8

16

16

16

16

8

8

16

16

16

16

16

16

8

8

8

8

8

8

8

8

8

At Recovery:
• If <COMMIT T> (randomly) written to the disk

8

8

8

• As before

16

8

• If not

• As on the next slide

16

16

16

16

8

8

16

<START T>

<T, A, 16>

<T, B, 16>

<COMMIT T>

Crash

43

Redo Logging – Recovery Example

Action

t

Mem A

Mem B

Disc A

Disc B

Log

READ(A,t)

t := t · 2

WRITE(A,t)

READ(B,t)

t := t · 2

WRITE(B,t)

FLUSH LOG

OUTPUT(A)

OUTPUT(B)

8

16

16

8

16

16

16

16

8

8

16

16

16

16

16

16

8

8

8

8

8

8

8

8

8

8

At Recovery:
• <COMMIT T> can not have been
   written to the disk
16
• Hence, T is incomplete (uncommitted)

8

8

8

8

• Do nothing at first

• Write <ABORT T> in log on the disk

16

16

16

16

8

16

<START T>

<T, A, 16>

<T, B, 16>

<COMMIT T>

Crash

44

Best of both Worlds

• Disadvantage Undo: Data must be written immediately after

the end of the transaction
– => Too many I/Os

• Disadvantage Redo: All changed blocks must be retained in

the buffer till COMMIT and log records are on the disk
– => High memory requirement

• Undo/Redo logging is more flexible

– But more information in log records is needed
– Not covered but you can google it!

45

Putting Everything
Together

Amazon’s DSQL

46

W/hat is Amazon DSQL?

• A geo-distributed database

• Guarantees Snapshot Isolation via MVCC and Timestamp Ordering

– Optimistic: assumes not a lot of transactions will conflict;
– Opposite assumption to e.g., Google Spanner

47

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Journal

Journal

Journal

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Storage Nodes

48

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Query Processor
• Assigns timestamps to transactions
Journal

(start/end)

• Timestamps are used for writing DB

versions to storage

• Queries storage nodes:

Journal

• Give me all data that committed

before I started execution (snapshot
isolation)

• Read-only transactions are answered

Journal
straight from storage (no
coordination)

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Storage Nodes

49

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Journal

Adjudicator

r
a
b
s
s
o
r
C

• Responsible for transactions (write path)
Journal
• Receives writes and read requests

r
a
b
s
s
o
r
C

• Does not write straight to storage

• Only logs changes to the Journal (log)

r
a
b
s
s
o
• Decides who gets to commit and who
r
C
aborts depending on timestamps

Journal

Storage Nodes

50

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Journal

r
a
b
s
s
o
r
C

Journal
• Responsible for storing logs/journals

• Each write is atomic and durable

Journal

(replicated)

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

• Contains all changes from the beginning of

time (i.e., like a REDO log)

Journal

• A transaction’s set of changes may span

multiple journals (thus, we need a crossbar)

Storage Nodes

51

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Journal

r
a
b
s
s
o
r
C

Crossbar
• Responsible for reading multiple journals

• A transaction may touch multiple keys
Journal
(keys are shared in the storage nodes
and adjudicators)

r
a
b
s
s
o
r
C

• Gathers changes for the storage nodes by
merging multiple journal entries and
forwarding them to the right shards

Journal

r
a
b
s
s
o
r
C

Storage Nodes

52

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Journal

r
a
b
s
s
o
r
C

Storage Nodes
• Store data and answer basic queries (e.g.,

scans, filters)
Journal

• Data is sharded/partitioned across multiple

storage nodes

• Storage nodes simply apply changes to

Journal

their files, coming from the crossbar/journal.

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

• Very “dumb”  nodes.

Storage Nodes

53

Logging + Timestamps =

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

r
o
t
a
c
i
d
u
d
A

j

Journal

Journal

Journal

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

r
a
b
s
s
o
r
C

Metronome

Query
Processor

d
n
e
t
n
o
r
F

Writes

Reads

Storage Nodes

54

Recap

• Timestamps are great, but hard to attain across regions

• TrueTime, atomic clocks and some tricks can tell you

which transaction came before another

• Logging allows you to store a complete database as a

log. Recover from failures, and rebuild your state

• Modern cloud systems make use of these concepts

into coherent, state-of-the-art systems

55

Next time

• Everything you always wanted to know about data

stream processing!

56

