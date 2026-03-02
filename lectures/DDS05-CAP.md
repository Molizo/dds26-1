Web-scale Data Management
CAP, GFS

Asterios Katsifodimos
George Christodoulou

1

Things you just can’t do #1

The CAP Theorem

“Brewer's conjecture and the feasibility of consistent, available, partition-tolerant web services”
Seth Gilbert, Nancy Lynch.
ACM SIGACT News, Volume 33, Issue 2 (2002)

2

Replicating Data Across Locations

X=5

X=5

X=5

3

Data Systems 101: Replication (advantages)

• Store copies of data on multiple nodes
• Improves scalability

• Parallel I/O and memory access

• Improves availability

• Node failures make only a replica of data unavailable

• Improves latency

• Data can be replicated across locations strategically

4

4

https://www.instagram.com/_yes_but

5

Data Systems 101: Replication – (disadvantages)

• Replicas need to be kept in sync

• Update require acks from all replicas to ensure

consistency

• Can we disconnect some of the nodes?

– CAP theorem discusses this.

6

CAP: (informal) Definitions

• Replica Consistency*

– All replicas return the same value
*Replica consistency nothing to do with Consistency in ACID.

• Availability

– Every non-failing node must return a response (in a reasonable

amount of time)

• Partition Tolerance

– In case of a network partition, the system still operates
– i.e., all machines that a client can access, will be operating

7

CAP Theorem

Let as assume replicas in USA, Europe, Australia

8

CAP Theorem Setting (happy scenario)

X=5

X=7

X=5

X=5

9

CAP Theorem Setting (happy scenario)

X=7

X=5

X=5

10

CAP Theorem Setting (happy scenario)

X=7

X=7

X=7

11

CAP Theorem Setting (failure scenario)

X=7

X=7

Network Partition

X=5

12

CAP Theorem: choose two

(Network)
Partition
tolerance

CP

PA

Consistensy

AC

Availability

13

CAP Theorem

System can be consistent and partition tolerant (CP)
BUT NOT  AVAILABLE

X=5

X=5

i.e., if asked for response, a non-failing node has the right to not respond.

14

X=7

CAP Theorem

Replicas can be available and partition tolerant
BUT NOT  CONSISTENT

X=5

X=5
X=5

X=7

15

CAP Theorem

System can be consistent and available (CA)
BUT CANNOT  AFFORD NETWORK PARTITION

X=5

X=5

How realistic is CA, really?
It depends: across datacenters, across
regions, across racks in the same
datacenter, etc.

X=5

16

Discussion

• What should you select: AP, CP, CA

– For a banking system storing account balances
– For a social networking system
– For shared document editing
– For a supply chain management system
– For a shopping cart system

17

ACID vs BASE

• ACID (relational databases)

– Atomicity
– Consistency
– Isolation
– Durability

• BASE

– Basically Available
– Soft State
– Eventually Consistent

18

18

ACID

ACID

BASE

Correctness

Scalability
Availability

19

Things you just
can’t do #2

The Byzantine Generals Problem

L. Lamport, R. Shostak, and M. Pease., “The Byzantine Generals Problem”, ACM Transactions on ProgrammingLanguagesand Systems

20

Byzantine Generals Problem

21

Byzantine Generals Problem

At what time are we attacking?

At 06:00 a.m.

22

Byzantine Generals Problem

At what time are we attacking?

At 06:00 a.m.

Simply, and Informally: the last sender of a message, cannot know if the message has been received.

23

Summary

• CAP Theorem

– You must choose between CP, AP, CA
– Depends on the use-case, and scenario
– Tradeoff between availability & consistency

• Byzantine Generals Problem

– Cannot know whether a service/node/database has received your

message.
• Unless it is acknowledged. But the Ack can be lost on the way, and the send

won’t know if you know.

24

Distributed Data Systems

See you next time!

26

