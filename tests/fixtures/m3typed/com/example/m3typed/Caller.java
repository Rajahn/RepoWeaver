package com.example.m3typed;

public class Caller {
    void dispatch(Shape shape) {
        shape.area();
    }

    void process(int value) {
        System.out.println(value);
    }

    void process(String value) {
        System.out.println(value);
    }

    void run() {
        Circle circle = new Circle(2.0);
        Square square = new Square(3.0);
        dispatch(circle);
        dispatch(square);
        process(1);
        process("one");
        Box<String> box = new Box<>("hi");
        box.identity("hi");
    }
}
