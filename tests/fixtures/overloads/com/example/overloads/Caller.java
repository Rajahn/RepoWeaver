package com.example.overloads;

public class Caller {
    void run(Codec codec, String json, Object something) {
        codec.fromJson(json, Foo.class);
        codec.write(null);
        codec.tag((String) something);
        codec.accept(new Foo());
        Box b1 = new Box(5);
        Box b2 = new Box(5L);
    }
}
